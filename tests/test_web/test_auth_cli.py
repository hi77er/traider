"""The CLI: the verbs that write the store, and the one that only reads it.

``python -m src.web.auth`` is the only way a store is ever created, which is what keeps the lock
off for a fresh checkout and for the suite. So the things worth pinning are the verbs' REFUSALS —
``set`` must not become an accidental reset, a wrong current PIN must not change anything, and
``status`` must never print a secret — rather than their happy paths, which the service tests
already cover.

The prompts are driven through the module's own ``_read_secret`` door, because ``getpass`` reads
the terminal rather than stdin and a test cannot type into one.
"""

from __future__ import annotations

import json

import pytest

from src.config.settings import Settings
from src.web import auth as auth_cli
from src.web.services import auth_service

PIN = "4821"
NEW_PIN = "7788"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        instrument="NVDA",
    )


@pytest.fixture()
def cli(settings, monkeypatch) -> list:
    """Answer the prompts from a script, and let the CLI read the tmp settings."""

    def run(*answers: str, argv=None) -> int:
        prompts: list = []

        def _read(prompt: str) -> str:
            prompts.append(prompt)
            return answers[min(len(prompts) - 1, len(answers) - 1)]

        monkeypatch.setattr(auth_cli, "get_settings", lambda: settings)
        monkeypatch.setattr(auth_cli, "_read_secret", _read)
        try:
            return auth_cli.main(list(argv) if argv else [])
        finally:
            pass

    return run


def _store(settings) -> dict:
    return json.loads(auth_service.store_path(settings).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------
def test_set_writes_the_store_and_turns_the_lock_on(cli, settings):
    assert auth_service.enabled(settings) is False

    assert cli(PIN, PIN, argv=["set"]) == 0

    assert auth_service.enabled(settings) is True
    assert auth_service.verify(settings, PIN)["ok"] is True


def test_set_refuses_a_pin_that_is_not_a_pin(cli, settings):
    assert cli("abc", "abc", argv=["set"]) == 1
    assert cli("12", "12", argv=["set"]) == 1, "too short"
    assert cli("1234567890123", "1234567890123", argv=["set"]) == 1, "too long"
    assert auth_service.enabled(settings) is False, "nothing was written"


def test_set_refuses_when_the_two_entries_do_not_match(cli, settings):
    assert cli(PIN, "9999", argv=["set"]) == 1
    assert auth_service.enabled(settings) is False


def test_set_will_not_overwrite_an_existing_pin(cli, settings):
    cli(PIN, PIN, argv=["set"])

    assert cli("1111", "1111", argv=["set"]) == 1, "the verb for that is 'reset'"
    assert auth_service.verify(settings, PIN)["ok"] is True, "and the old PIN still works"


def test_change_needs_the_current_pin_and_signs_the_others_out(cli, settings):
    cli(PIN, PIN, argv=["set"])
    before = auth_service.issue(settings)

    assert cli("0000", NEW_PIN, NEW_PIN, argv=["change"]) == 1, "wrong current PIN"
    assert auth_service.verify(settings, PIN)["ok"] is True, "nothing changed"

    assert cli(PIN, NEW_PIN, NEW_PIN, argv=["change"]) == 0
    assert auth_service.verify(settings, NEW_PIN)["ok"] is True
    assert auth_service.read(settings, before) is None, "the old session is dead"


def test_reset_needs_no_current_pin(cli, settings):
    cli(PIN, PIN, argv=["set"])

    assert cli(NEW_PIN, NEW_PIN, argv=["reset"]) == 0

    assert auth_service.verify(settings, NEW_PIN)["ok"] is True
    assert auth_service.verify(settings, PIN)["ok"] is False


def test_disable_turns_the_lock_off(cli, settings):
    cli(PIN, PIN, argv=["set"])

    assert cli(argv=["disable"]) == 0

    assert auth_service.enabled(settings) is False
    assert auth_service.verify(settings, PIN)["ok"] is False


# ---------------------------------------------------------------------------
# what a reader is told
# ---------------------------------------------------------------------------
def test_status_never_prints_a_secret(cli, settings, capsys):
    cli(PIN, PIN, argv=["set"])
    raw = _store(settings)

    assert cli(argv=["status"]) == 0
    printed = capsys.readouterr().out

    assert '"enabled": true' in printed and "owner" in printed
    assert raw["secret"] not in printed
    assert raw["machine_token"] not in printed
    assert raw["users"][0]["hash"] not in printed
    assert raw["users"][0]["salt"] not in printed
    assert PIN not in printed, "not even the PIN that was just set"


def test_status_says_the_lock_is_off_when_there_is_no_store(cli, capsys):
    assert cli(argv=["status"]) == 0
    assert "The lock is OFF" in capsys.readouterr().out


def test_a_help_line_or_an_unknown_verb_does_not_touch_anything(cli, settings, capsys):
    assert cli(argv=["help"]) == 0
    assert "usage:" in capsys.readouterr().out
    assert cli(argv=["nonsense"]) == 2, "a usage error, not a change"
    assert auth_service.enabled(settings) is False
