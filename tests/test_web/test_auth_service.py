"""The portal's PIN: the store, the lockout, and the cookie that outlives a page load.

Everything here is the service, driven directly — the middleware, the routes and the CLI are thin
wrappers over it, and each is tested for its own half. Three properties get the most attention,
because they are the ones a quiet mistake would ruin:

* the PIN is never readable from what is stored;
* the lockout is PERSISTED, so a restart is not a fresh budget of guesses;
* a PIN change signs every existing session out — and the trading switch is untouched by all of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.config.settings import Settings
from src.config.trading_state import write_state
from src.web.services import auth_service

PIN = "4821"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="NVDA",
    )


def _at(minutes: float = 0) -> datetime:
    return datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


@pytest.fixture()
def armed(settings) -> Settings:
    """A store with one user, written the way the CLI writes it."""
    auth_service.create(settings, PIN, label="NVDA", at=_at())
    return settings


def _store(settings) -> dict:
    return json.loads(auth_service.store_path(settings).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def test_there_is_no_lock_until_somebody_creates_one(settings):
    """The whole on/off switch is the file's existence, so a fresh checkout is unaffected."""
    assert auth_service.enabled(settings) is False
    assert auth_service.load(settings) is None
    assert auth_service.verify(settings, PIN)["ok"] is False
    assert auth_service.issue(settings) is None, "and there is nobody to issue a session to"


def test_the_pin_is_not_readable_from_what_is_stored(armed):
    """A stretched hash over a per-install salt, with the parameters on the record for later."""
    raw = auth_service.store_path(armed).read_text(encoding="utf-8")
    user = _store(armed)["users"][0]

    assert PIN not in raw, "the PIN must not appear in the file at all"
    assert len(user["salt"]) == 32 and len(user["hash"]) == 64
    assert user["kdf"] == auth_service.KDF
    assert user["generation"] == 1 and user["failed_attempts"] == 0
    assert _store(armed)["version"] == 1
    assert len(_store(armed)["secret"]) == 64, "the cookie-signing key is per-install"


def test_the_store_lands_beside_the_switch_and_not_on_it(armed):
    """Trading state and lock state must never be the same file."""
    path = auth_service.store_path(armed)

    assert path.name == "auth.json"
    assert path.parent.name == "data"
    assert path != (path.parent / "trading.json"), "the switch is a different file entirely"


def test_a_second_set_refuses_rather_than_overwrites(armed):
    """``set`` cannot quietly become "reset" — that is the verb that says it out loud."""
    with pytest.raises(FileExistsError):
        auth_service.create(armed, "9999", at=_at())


def test_disable_removes_it_and_says_whether_there_was_one(armed):
    assert auth_service.disable(armed) is True
    assert auth_service.enabled(armed) is False
    assert auth_service.disable(armed) is False, "nothing to remove the second time"


# ---------------------------------------------------------------------------
# checking a PIN, and the lockout
# ---------------------------------------------------------------------------
def test_the_right_pin_verifies_and_the_wrong_one_does_not(armed):
    assert auth_service.verify(armed, PIN, at=_at())["ok"] is True

    wrong = auth_service.verify(armed, "0000", at=_at())
    assert wrong["ok"] is False
    assert "4 attempt" in wrong["reason"], "the operator is told what is left"


def test_five_wrong_pins_lock_it_and_the_correct_one_is_refused_while_locked(armed):
    for attempt in range(auth_service.MAX_ATTEMPTS):
        result = auth_service.verify(armed, "0000", at=_at(attempt))
    assert result["locked"] is True
    assert "locked for" in result["reason"]

    # The count is spent: the RIGHT PIN does not get in either, which is the point of a lockout.
    assert auth_service.verify(armed, PIN, at=_at(6))["ok"] is False
    assert auth_service.verify(armed, PIN, at=_at(7))["locked"] is True


def test_the_lockout_survives_a_restart_and_then_expires(armed):
    """Persisted on purpose: reloading the process must not hand out a fresh set of guesses."""
    for attempt in range(auth_service.MAX_ATTEMPTS):
        auth_service.verify(armed, "0000", at=_at(attempt))
    assert _store(armed)["users"][0]["locked_until"] is not None, "written down, not held in RAM"

    still = auth_service.verify(armed, PIN, at=_at(2))
    assert still["ok"] is False and still["locked"] is True

    # The lock began with the fifth attempt, and lasts LOCKOUT_SECONDS from there.
    after = auth_service.verify(
        armed,
        PIN,
        at=_at(auth_service.MAX_ATTEMPTS + auth_service.LOCKOUT_SECONDS / 60 + 1),
    )
    assert after["ok"] is True, "and it is a delay, not a permanent refusal"
    assert _store(armed)["users"][0]["locked_until"] is None


def test_a_good_pin_clears_the_failures_behind_it(armed):
    for attempt in range(2):
        auth_service.verify(armed, "0000", at=_at(attempt))
    assert _store(armed)["users"][0]["failed_attempts"] == 2

    assert auth_service.verify(armed, PIN, at=_at(2))["ok"] is True
    assert _store(armed)["users"][0]["failed_attempts"] == 0


# ---------------------------------------------------------------------------
# the session cookie
# ---------------------------------------------------------------------------
def test_a_session_round_trips_and_is_refused_when_it_is_tampered_with(armed):
    token = auth_service.issue(armed, at=_at())
    assert auth_service.read(armed, token, at=_at(1))["sub"] == "owner"

    body, _, mac = token.partition(".")
    assert auth_service.read(armed, f"{body}x.{mac}", at=_at(1)) is None, "body changed"
    assert auth_service.read(armed, f"{body}.{mac[:-1]}x", at=_at(1)) is None, "signature changed"
    assert auth_service.read(armed, "nonsense", at=_at(1)) is None
    assert auth_service.read(armed, None, at=_at(1)) is None


def test_a_session_goes_stale_when_it_is_not_used(armed):
    """The idle window is the browser's inactivity lock's server half."""
    token = auth_service.issue(armed, at=_at())
    idle_minutes = _store(armed)["idle_seconds"] / 60

    assert auth_service.read(armed, token, at=_at(idle_minutes - 1)) is not None
    assert auth_service.read(armed, token, at=_at(idle_minutes + 1)) is None


def test_a_busy_session_still_ends_at_the_absolute_cap(armed):
    """Sliding cannot extend a session for ever: a day after the login, it is over."""
    token = auth_service.issue(armed, at=_at())
    # Used every ten minutes right up to the cap — inside the idle window, so the session stays
    # alive and only the absolute clock can end it.
    for minutes in range(0, 24 * 60 + 1, 10):
        token = auth_service.touch(armed, token, at=_at(minutes))
        assert token is not None, f"the session should still be usable at minute {minutes}"

    assert auth_service.read(armed, token, at=_at(24 * 60)) is not None, "still inside"
    assert auth_service.read(armed, token, at=_at(24 * 60 + 1)) is None, "one minute past it"


def test_touching_a_session_moves_only_its_idle_clock(armed):
    token = auth_service.issue(armed, at=_at())
    moved = auth_service.touch(armed, token, at=_at(10))

    assert moved != token
    assert auth_service.read(armed, moved, at=_at(20)) is not None, "the new one is live"
    assert auth_service.read(armed, token, at=_at(16)) is None, "the old idle clock still runs"
    assert auth_service.touch(armed, "nonsense", at=_at(10)) is None


# ---------------------------------------------------------------------------
# changing it
# ---------------------------------------------------------------------------
def test_changing_the_pin_needs_the_current_one(armed):
    refused = auth_service.change_pin(armed, "0000", "7788", at=_at())
    assert refused["ok"] is False
    assert auth_service.verify(armed, PIN, at=_at(1))["ok"] is True, "and nothing changed"


def test_changing_the_pin_signs_every_existing_session_out(armed):
    """The reason ``generation`` and ``secret`` are on the record at all."""
    before = auth_service.issue(armed, at=_at())
    assert auth_service.read(armed, before, at=_at(1)) is not None

    changed = auth_service.change_pin(armed, PIN, "7788", at=_at(2))

    assert changed["ok"] is True and changed["generation"] == 2
    assert auth_service.read(armed, before, at=_at(3)) is None, "the old cookie is dead"
    assert auth_service.verify(armed, PIN, at=_at(3))["ok"] is False, "and so is the old PIN"
    assert auth_service.verify(armed, "7788", at=_at(3))["ok"] is True
    assert auth_service.issue(armed, at=_at(3)) is not None, "a new session can be handed out"


def test_reset_needs_no_current_pin_and_also_signs_everyone_out(armed):
    before = auth_service.issue(armed, at=_at())

    result = auth_service.reset(armed, "1122", at=_at(5))

    assert result["ok"] is True
    assert auth_service.read(armed, before, at=_at(6)) is None
    assert auth_service.verify(armed, "1122", at=_at(6))["ok"] is True
    assert _store(armed)["users"][0]["generation"] == 2


def test_reset_on_a_portal_with_no_pin_just_sets_one(settings):
    assert auth_service.reset(settings, "4321", at=_at())["ok"] is True
    assert auth_service.verify(settings, "4321", at=_at())["ok"] is True


def test_a_wrong_current_pin_feeds_the_same_lockout(armed):
    """The change endpoint asks for the same secret, so it must cost the same guesses."""
    for attempt in range(auth_service.MAX_ATTEMPTS):
        auth_service.change_pin(armed, "0000", "7788", at=_at(attempt))

    assert auth_service.change_pin(armed, PIN, "7788", at=_at(6))["ok"] is False, (
        "the correct PIN does not get through a lockout opened by the other door"
    )


# ---------------------------------------------------------------------------
# the machine token, and the invariant about the loop
# ---------------------------------------------------------------------------
def test_the_machine_token_is_its_own_secret(armed):
    token = _store(armed)["machine_token"]
    assert auth_service.machine_ok(armed, token) is True
    assert auth_service.machine_ok(armed, "nope") is False
    assert auth_service.machine_ok(armed, None) is False


def test_a_pin_change_leaves_the_machine_token_alone(armed):
    """Rotating it would silently break the cron that restores a crashed loop."""
    before = _store(armed)["machine_token"]
    auth_service.change_pin(armed, PIN, "7788", at=_at())
    assert _store(armed)["machine_token"] == before


def test_none_of_this_touches_the_trading_switch(settings):
    """The invariant the whole design rests on: the loop reads that file, and not this module."""
    write_state(settings, {"on": True, "strategy": "NVDA", "env": "paper"})
    switch = auth_service.store_path(settings).parent / "trading.json"
    before = switch.read_text(encoding="utf-8")

    auth_service.create(settings, PIN, at=_at())
    auth_service.verify(settings, "0000", at=_at(1))
    for attempt in range(auth_service.MAX_ATTEMPTS):
        auth_service.verify(settings, "0000", at=_at(2 + attempt))
    auth_service.change_pin(settings, PIN, "7788", at=_at(9))
    auth_service.disable(settings)

    assert switch.read_text(encoding="utf-8") == before, "the switch is not auth's business"


def test_describe_prints_no_secret(armed):
    """What ``status`` shows: what is set and since when — never how."""
    described = auth_service.describe(armed)
    printed = json.dumps(described, sort_keys=True)

    assert described["enabled"] is True and described["user"] == "owner"
    assert described["generation"] == 1 and described["updated_at"]
    assert described["machine_token"] is True, "that one exists, not what it is"
    raw = _store(armed)
    assert raw["secret"] not in printed and raw["machine_token"] not in printed
    assert raw["users"][0]["hash"] not in printed and raw["users"][0]["salt"] not in printed


# ---------------------------------------------------------------------------
# what a PIN may be, and the three ways one is written
# ---------------------------------------------------------------------------
def test_pin_problem_refuses_the_shapes_everyone_tries_first():
    """A nudge, not a policy — and the same answer the lock screen gives without asking us."""
    for weak in ("1234", "0000", "7777777", "123456"):
        assert auth_service.pin_problem(weak), weak
    for bad in ("", "abc", "12", "4821x", "1234567890123"):
        assert auth_service.pin_problem(bad), bad
    for fine in (PIN, "135790", "918273645"):
        assert auth_service.pin_problem(fine) is None, fine


def test_create_refuses_a_weak_first_pin_and_writes_nothing(settings):
    result = auth_service.create(settings, "1234", at=_at())

    assert result["ok"] is False and "first PIN" in result["reason"]
    assert auth_service.enabled(settings) is False, "and the portal is not half-locked"


def test_reset_refuses_a_weak_pin_too(armed):
    assert auth_service.reset(armed, "9999", at=_at())["ok"] is False
    assert auth_service.verify(armed, PIN, at=_at())["ok"] is True


def test_change_passes_a_lockout_through_rather_than_calling_it_a_wrong_pin(armed):
    """The route turns this into 429, so it has to arrive even though the current PIN was right."""
    for attempt in range(auth_service.MAX_ATTEMPTS):
        auth_service.verify(armed, "0000", at=_at(attempt))

    refused = auth_service.change_pin(armed, PIN, "7788", at=_at(6))

    assert refused["ok"] is False and refused["locked"] is True
    assert refused["locked_until"], "and the caller can say how long"


def test_change_to_the_pin_you_have_rotates_nothing(armed):
    before = _store(armed)
    token = str(auth_service.issue(armed, at=_at()))

    result = auth_service.change_pin(armed, PIN, PIN, at=_at(1))

    assert result["ok"] is True and result["unchanged"] is True
    after = _store(armed)
    assert after["secret"] == before["secret"], "no session was invalidated"
    assert after["users"][0]["generation"] == before["users"][0]["generation"]
    assert auth_service.read(armed, token, at=_at(2)) is not None, "and that cookie still works"


def test_sign_out_others_keeps_the_pin_and_kills_the_cookies(armed):
    other = str(auth_service.issue(armed, at=_at()))
    assert auth_service.read(armed, other, at=_at(1)) is not None

    result = auth_service.sign_out_others(armed, at=_at(2))

    assert result["ok"] is True and result["generation"] == 2
    assert auth_service.read(armed, other, at=_at(3)) is None
    assert auth_service.verify(armed, PIN, at=_at(3))["ok"] is True, "the PIN is not the session"
    fresh = auth_service.issue(armed, at=_at(3))
    assert auth_service.read(armed, fresh, at=_at(4)) is not None, "and a new session works"


def test_sign_out_others_needs_a_store(settings):
    assert auth_service.sign_out_others(settings)["ok"] is False


# ---------------------------------------------------------------------------
# the idle window: a SETTING an operator changes, not a constant
# ---------------------------------------------------------------------------
def _with_idle(settings, minutes: int) -> Settings:
    return settings.model_copy(update={"auth_idle_minutes": minutes})


def test_the_idle_window_comes_from_the_setting(armed):
    """Account Settings owns it, so changing it takes effect without a new login."""
    token = str(auth_service.issue(armed, at=_at()))

    short = _with_idle(armed, 1)
    assert auth_service.idle_seconds(short) == 60
    assert auth_service.read(short, token, at=_at(1.5)) is None, "90s is past a 1-minute window"

    long = _with_idle(armed, 30)
    assert auth_service.idle_seconds(long) == 1800
    assert auth_service.read(long, token, at=_at(1.5)) is not None, "and inside a 30-minute one"
    assert auth_service.read(long, token, at=_at(31)) is None, "but not past it"


def test_the_setting_is_what_the_login_and_the_status_report(armed):
    """The browser locks on the number the server tells it, so both answers must be this one."""
    five = _with_idle(armed, 5)

    assert auth_service.verify(five, PIN, at=_at())["idle_seconds"] == 300
    assert auth_service.describe(five)["idle_seconds"] == 300


def test_a_store_written_before_the_setting_existed_still_has_its_window(armed):
    """``auth.json`` carries the window it was created with; only an object without the field
    falls back to it, which is the deployment that never opens Account Settings."""
    class Bare:
        """A settings object from before the field existed — no ``auth_idle_minutes``."""

    assert auth_service.idle_seconds(Bare(), auth_service.load(armed)) == (
        auth_service.DEFAULT_IDLE_SECONDS
    )
