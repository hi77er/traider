"""Verifying Alpaca credentials: is this key pair *working*, or merely present?

Having keys configured is not the same as being able to trade with them — they get
copied from the wrong account page, revoked, or given the paper secret of a live
key. Nothing local can tell the difference, so the broker is asked and the answer is
remembered.

What these tests pin down, in the order the rules matter:

* the HTTP mapping: 200 means verified, 401/403 means rejected, anything else (and
  any transport failure) means "could not verify" — never "verified";
* the secret never reaches the file, a message, or the log;
* a verdict belongs to the KEY that earned it, so swapping keys expires it;
* a pass is cached, a failure is not (a failure may be a blip);
* the store is private, atomic, and unreadable when corrupt.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import state_files
from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.execution import credentials
from src.execution.config import LIVE_BASE_URL, PAPER_BASE_URL
from src.web.app import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]

PAPER = {"alpaca_paper_api_key": "PK-paper", "alpaca_paper_api_secret": "PS-paper"}
LIVE = {"alpaca_live_api_key": "LK-live", "alpaca_live_api_secret": "LS-live"}


def _s(tmp_path, **kwargs) -> Settings:
    return Settings(_env_file=None, data_dir=str(tmp_path / "data"), **kwargs)


@pytest.fixture(autouse=True)
def store_in_tmp(tmp_path, monkeypatch):
    """No test may write into the repo's real data directory."""
    monkeypatch.setattr(state_files, "state_path", lambda s, name: tmp_path / name)
    return tmp_path


class FakeResponse:
    def __init__(self, status_code, body=None, raises=None):
        self.status_code = status_code
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises:
            raise self._raises
        return self._body


@pytest.fixture
def http(monkeypatch):
    """A stand-in for ``requests.get`` that records the calls it received."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if fake_get.transport_error is not None:
            raise fake_get.transport_error
        return fake_get.response

    fake_get.response = FakeResponse(200, {"account_number": "PA3TEST", "status": "ACTIVE"})
    fake_get.transport_error = None
    fake_get.calls = calls
    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    return fake_get


# ---------------------------------------------------------------------------
# the broker call
# ---------------------------------------------------------------------------
def test_a_200_verifies_and_reports_which_account_answered(http):
    got = credentials.probe("PK-paper", "PS-paper", PAPER_BASE_URL)
    assert got["ok"] is True
    assert got["account_number"] == "PA3TEST" and got["status"] == "ACTIVE"
    # The proof is that we reached the RIGHT endpoint with the RIGHT headers.
    assert http.calls[0]["url"] == f"{PAPER_BASE_URL}/v2/account"
    assert http.calls[0]["headers"]["APCA-API-KEY-ID"] == "PK-paper"
    assert http.calls[0]["headers"]["APCA-API-SECRET-KEY"] == "PS-paper"
    assert http.calls[0]["timeout"] == credentials.VERIFY_TIMEOUT_SECONDS


def test_the_live_environment_is_checked_against_the_live_endpoint(http):
    credentials.probe("LK-live", "LS-live", LIVE_BASE_URL)
    assert http.calls[0]["url"] == f"{LIVE_BASE_URL}/v2/account"


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_keys_say_which_account_type_to_check(http, code):
    http.response = FakeResponse(code, {"message": "unauthorized"})
    got = credentials.probe("PK-paper", "PS-paper", PAPER_BASE_URL)
    assert got["ok"] is False
    # The single most common cause is a key from the other account type, so the
    # message names it rather than leaving a bare 401.
    assert "paper and live keys are different" in got["message"]
    assert str(code) in got["message"]


def test_an_unexpected_status_is_not_treated_as_verified(http):
    http.response = FakeResponse(500, {})
    got = credentials.probe("PK-paper", "PS-paper", PAPER_BASE_URL)
    assert got["ok"] is False and "500" in got["message"]


def test_a_transport_failure_is_not_treated_as_verified(http):
    """A refused connection must not read as a pass — and must not raise either,
    because the caller is rendering a settings panel."""
    http.transport_error = OSError("connection refused")
    try:
        got = credentials.probe("PK-paper", "PS-paper", PAPER_BASE_URL)
    except Exception as exc:  # pragma: no cover - would fail the assertion below
        pytest.fail(f"probe must never raise, but raised {exc!r}")
    assert got["ok"] is False
    assert "Could not reach" in got["message"]
    assert "connection refused" in got["message"]


def test_a_garbled_200_body_still_verifies_without_inventing_details(http):
    http.response = FakeResponse(200, raises=ValueError("not json"))
    got = credentials.probe("PK-paper", "PS-paper", PAPER_BASE_URL)
    assert got["ok"] is True and got["account_number"] == ""


def test_a_transport_error_cannot_leak_the_secret(http):
    http.transport_error = OSError("failed for PS-secret-value")
    got = credentials.probe("PK-paper", "PS-secret-value", PAPER_BASE_URL)
    assert "PS-secret-value" not in got["message"]


# ---------------------------------------------------------------------------
# the verdict store
# ---------------------------------------------------------------------------
def test_the_fingerprint_is_stable_per_environment_and_never_the_key(tmp_path):
    a = credentials.fingerprint("paper", "PK-1")
    assert a == credentials.fingerprint("paper", "PK-1")
    assert a != credentials.fingerprint("paper", "PK-2")
    # The same key id in two environments is two different pairs.
    assert a != credentials.fingerprint("live", "PK-1")
    assert "PK-1" not in a


def test_nothing_secret_is_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"})
    credentials.verify(_s(tmp_path, **PAPER), "paper", force=True)
    raw = credentials.state_path(_s(tmp_path)).read_text(encoding="utf-8")
    assert "PS-paper" not in raw and "PK-paper" not in raw
    assert "fingerprint" in raw


def test_the_verdict_file_is_private_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"})
    settings = _s(tmp_path, **PAPER)
    credentials.verify(settings, "paper", force=True)
    path = credentials.state_path(settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.glob(".*tmp")] == []


def test_an_unreadable_verdict_file_is_not_mistaken_for_verified(tmp_path):
    settings = _s(tmp_path, **PAPER)
    credentials.state_path(settings).write_text("{ not json", encoding="utf-8")
    assert credentials.check_for(settings, "paper")["verified"] is False


def test_a_missing_verdict_file_means_not_verified(tmp_path):
    settings = _s(tmp_path, **PAPER)
    assert not credentials.state_path(settings).exists()
    state = credentials.check_for(settings, "paper")
    assert state["verified"] is False and state["keys_set"] is True and state["checked_at"] is None


def test_check_for_reports_which_pair_is_missing(tmp_path):
    assert credentials.check_for(_s(tmp_path), "paper")["keys_set"] is False
    assert credentials.check_for(_s(tmp_path, **PAPER), "paper")["keys_set"] is True


def test_verify_refuses_to_check_half_a_pair(tmp_path):
    got = credentials.verify(_s(tmp_path, alpaca_paper_api_key="PK-only"), "paper")
    assert got["ok"] is False and got["checked"] is False
    assert "key and its secret are needed" in got["message"]
    assert not credentials.state_path(_s(tmp_path)).exists(), "nothing to record"


def test_verify_reports_when_there_is_nothing_at_all_to_check(tmp_path):
    got = credentials.verify(_s(tmp_path), "live")
    assert got["ok"] is False and got["checked"] is False
    assert "No LIVE credentials found to validate" in got["message"]
    assert got["has_verdict"] is False, "nothing was examined, so there is no verdict"


def test_an_unknown_environment_is_rejected():
    with pytest.raises(ValueError):
        credentials.keys_for(Settings(_env_file=None), "production")


def test_require_verified_explains_each_of_the_three_refusals(tmp_path, monkeypatch):
    settings = _s(tmp_path, **PAPER)
    assert "have not been verified yet" in credentials.require_verified(settings, "paper")

    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": False, "message": "401 nope"})
    credentials.verify(settings, "paper", force=True)
    assert "FAILED verification" in credentials.require_verified(settings, "paper")

    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"})
    credentials.verify(settings, "paper", force=True)
    assert credentials.require_verified(settings, "paper") is None


# ---------------------------------------------------------------------------
# the schema + the API
# ---------------------------------------------------------------------------
def test_the_account_schema_carries_the_verdict_and_a_verify_hook(tmp_path, monkeypatch):
    from src.web.services import config_service

    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    schema = config_service.get_account_schema()
    assert set(schema["credentials"]) == {"paper", "live"}
    # Exactly one Validate affordance per pair, on the LAST field of the pair, so the
    # control lands under the two boxes it checks — and naming both of them, so no
    # client has to guess which inputs make a pair.
    hooks = [f for f in schema["groups"][0]["fields"] if f.get("verify")]
    assert [h["key"] for h in hooks] == ["ALPACA_PAPER_API_SECRET", "ALPACA_LIVE_API_SECRET"]
    assert [h["verify"] for h in hooks] == [
        {"env": "paper", "key_key": "ALPACA_PAPER_API_KEY", "secret_key": "ALPACA_PAPER_API_SECRET"},
        {"env": "live", "key_key": "ALPACA_LIVE_API_KEY", "secret_key": "ALPACA_LIVE_API_SECRET"},
    ]


def test_the_verdict_distinguishes_having_no_verdict_from_failing(tmp_path, monkeypatch):
    """The popup shows a badge only for a verdict about the pair in play, so "nobody
    has checked this yet" must be a different answer from "this failed"."""
    settings = _s(tmp_path, **PAPER)
    assert credentials.check_for(settings, "paper")["has_verdict"] is False

    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": False, "message": "401 nope"})
    credentials.verify(settings, "paper", force=True)
    state = credentials.check_for(settings, "paper")
    assert state["has_verdict"] is True and state["verified"] is False
    assert state["checked_at"] and state["message"] == "401 nope"

    # A different key has no verdict of its own — the old failure is not about it.
    other = _s(tmp_path, alpaca_paper_api_key="PK-other", alpaca_paper_api_secret="PS-other")
    fresh = credentials.check_for(other, "paper")
    assert fresh["has_verdict"] is False and fresh["message"] == ""
    assert fresh["stale"] is True, "but we know a verdict exists for something else"


def test_the_verify_endpoint_reports_a_pass_and_a_failure(tmp_path, monkeypatch):
    from src.web.services import config_service

    settings = _s(tmp_path, **PAPER)
    # The popup's endpoint reads the effective settings itself, and its reply carries
    # the verdicts for the STORED pair — so the pair being validated has to be the
    # stored one for the two halves of the response to agree.
    monkeypatch.setattr(config_service, "get_effective_settings", lambda: settings)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "Credentials accepted", "account_number": "A1", "status": "ACTIVE"})
        good = client.post(
            "/api/v1/account/verify",
            json={"env": "paper", "key_id": PAPER["alpaca_paper_api_key"], "secret": PAPER["alpaca_paper_api_secret"]},
        ).json()
        assert good["ok"] is True and good["result"]["verified"] is True
        assert good["credentials"]["paper"]["verified"] is True

        monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": False, "message": "Alpaca rejected these credentials (401)"})
        bad = client.post(
            "/api/v1/account/verify",
            json={"env": "paper", "key_id": PAPER["alpaca_paper_api_key"], "secret": PAPER["alpaca_paper_api_secret"]},
        ).json()
        assert bad["ok"] is False and "401" in bad["message"]
        assert bad["credentials"]["paper"]["verified"] is False
    finally:
        app.dependency_overrides.clear()


def test_the_verify_endpoint_rejects_an_unknown_environment(tmp_path):
    settings = _s(tmp_path, **PAPER)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.post("/api/v1/account/verify", json={"env": "production"}).json()
        assert body["ok"] is False and "paper" in body["message"]
    finally:
        app.dependency_overrides.clear()


def test_the_verify_endpoint_checks_the_values_in_the_boxes(tmp_path, monkeypatch):
    """The reported bug: keys typed into the popup were answered with "credentials
    are not supplied", because the endpoint looked only at the SAVED pair. A pair is
    normally typed first and stored afterwards."""
    seen = []

    def fake_probe(key_id, secret, base_url, **kw):
        seen.append((key_id, secret, base_url))
        return {"ok": True, "message": "Credentials accepted", "account_number": "A9", "status": "ACTIVE"}

    settings = _s(tmp_path)  # nothing saved at all
    monkeypatch.setattr(credentials, "probe", fake_probe)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.post(
            "/api/v1/account/verify",
            json={"env": "live", "key_id": "LK-typed", "secret": "LS-typed"},
        ).json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is True, body["message"]
    assert seen == [("LK-typed", "LS-typed", credentials.keys_for(settings, "live")["base_url"])]
    result = body["result"]
    assert result["checked"] is True and result["has_verdict"] is True
    assert result["saved"] is False, "valid, but not what the bot would trade with yet"
    # And the verdict belongs to what was checked, so it is already usable if the
    # popup is saved with these very values.
    assert credentials.check_for(settings, "live")["message"] == ""


def test_a_pass_on_unsaved_values_survives_saving_them(tmp_path, monkeypatch):
    """Typing a pair, validating it, and then saving must not cost a second call to
    Alpaca — the fingerprint of the checked pair is the same one."""
    from src.web.services import config_service

    calls = []

    def fake_probe(key_id, secret, base_url, **kw):
        calls.append(key_id)
        return {"ok": True, "message": "Credentials accepted", "account_number": "A9", "status": "ACTIVE"}

    settings = _s(tmp_path)
    monkeypatch.setattr(credentials, "probe", fake_probe)
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    assert config_service.verify_credentials("live", "LK-typed", "LS-typed")["ok"] is True
    assert len(calls) == 1

    # Saving a pair that was already proved does not ask again.
    saved = _s(tmp_path, alpaca_live_api_key="LK-typed", alpaca_live_api_secret="LS-typed")
    out = credentials.verify_new(saved)
    assert out["live"]["verified"] is True and out["live"]["checked"] is False
    assert len(calls) == 1


def test_a_half_filled_form_says_which_half_is_missing(tmp_path, monkeypatch):
    """One box filled and the other blank is the common paste mistake, so it gets an
    answer that names what is missing rather than a generic refusal."""
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: pytest.fail("nothing to check"))
    empty = _s(tmp_path)
    app.dependency_overrides[get_effective_settings_dep] = lambda: empty
    try:
        body = client.post(
            "/api/v1/account/verify",
            json={"env": "live", "key_id": "LK-typed", "secret": ""},
        ).json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is False and body["result"]["checked"] is False
    assert "key and its secret are needed" in body["message"]
    assert "LK-typed" not in body["message"], "no credential comes back out"


def test_a_masked_box_is_nothing_to_validate(tmp_path, monkeypatch):
    """A re-opened popup shows "********" instead of the secret. Echoing that back
    must be answered as "nothing to validate" — never by quietly checking the stored
    pair, or by asking Alpaca to authenticate asterisks."""
    from src.web.services import config_service

    settings = _s(tmp_path, alpaca_live_api_key="LK-saved", alpaca_live_api_secret="LS-saved")
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    monkeypatch.setattr(config_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: pytest.fail("nothing to check"))

    for key_id, secret in (("********", "********"), ("", ""), (None, None)):
        got = config_service.verify_credentials("live", key_id, secret)
        assert got["ok"] is False and got["result"]["checked"] is False, (key_id, secret)
        assert "No LIVE credentials found to validate" in got["message"]


def test_a_blank_field_is_not_silently_filled_from_the_stored_pair(tmp_path, monkeypatch):
    """Supplying form values means checking the FORM. Half of it is half of it — the
    stored secret must not be substituted, because then an empty box would produce a
    verdict about a credential that is not on screen."""
    settings = _s(tmp_path, alpaca_live_api_key="LK-saved", alpaca_live_api_secret="LS-saved")
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: pytest.fail("the stored secret must not be used"))
    got = credentials.verify(settings, "live", force=True, key_id="LK-new", secret="")
    assert got["ok"] is False and got["checked"] is False
    assert "key and its secret are needed" in got["message"]


def test_omitting_the_arguments_checks_the_stored_pair(tmp_path, monkeypatch):
    """...while the callers that care about the pair the BOT would use — the save-time
    check and turning trading on — get exactly that pair."""
    settings = _s(tmp_path, alpaca_live_api_key="LK-saved", alpaca_live_api_secret="LS-saved")
    seen = []

    def fake_probe(key_id, secret, url, **kw):
        seen.append((key_id, secret))
        return {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"}

    monkeypatch.setattr(credentials, "probe", fake_probe)
    assert credentials.verify(settings, "live", force=True)["ok"] is True
    assert seen == [("LK-saved", "LS-saved")]


def test_validating_an_empty_paper_form_reports_nothing_to_validate(tmp_path, monkeypatch):
    """The reported bug: pressing Validate on the paper row with an empty form
    announced a rejection. It had silently fallen back to the stored pair, so it was
    reporting on a credential that was not on screen."""
    from src.web.services import config_service

    settings = _s(tmp_path, **PAPER)  # a stored pair exists...
    monkeypatch.setattr(config_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: pytest.fail("an empty form is not a request to Alpaca"))
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.post("/api/v1/account/verify", json={"env": "paper"}).json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is False
    assert body["message"] == (
        "No PAPER credentials found to validate — enter the API key and its secret."
    )
    assert body["result"]["checked"] is False and body["result"]["has_verdict"] is False
    assert credentials.check_for(settings, "paper")["has_verdict"] is False, "no verdict was earned"


def test_typing_credentials_that_are_wrong_still_says_invalid(tmp_path, monkeypatch):
    """The other half of the rule: once something IS entered, it is checked and a
    rejection is reported as a rejection."""
    from src.web.services import config_service

    settings = _s(tmp_path, **PAPER)
    monkeypatch.setattr(config_service, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(
        credentials, "probe",
        lambda key_id, secret, url, **k: {"ok": False, "message": "Alpaca rejected these credentials (401)"},
    )
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        body = client.post(
            "/api/v1/account/verify",
            json={"env": "paper", "key_id": "PK-typed", "secret": "PS-typed"},
        ).json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is False and body["result"]["checked"] is True
    assert "NOT valid" not in body["message"], "the endpoint states it plainly..."
    assert "401" in body["message"]


def test_the_verify_endpoint_works_even_while_trading_is_on(tmp_path, monkeypatch):
    """Re-checking a key is not reconfiguring the bot, and a revoked key must be
    discoverable rather than hidden behind the lock."""
    settings = _s(tmp_path, **PAPER)
    monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"})
    from src.web.services import trading_service

    credentials.verify(settings, "paper", force=True)
    trading_service.turn_on(settings)
    try:
        app.dependency_overrides[get_effective_settings_dep] = lambda: settings
        assert trading_service.is_trading_on(settings) is True
        assert client.post("/api/v1/account/verify", json={"env": "paper"}).status_code == 200
    finally:
        app.dependency_overrides.clear()
        trading_service.turn_off(settings)


def test_saving_the_account_verifies_a_newly_added_pair(tmp_path, monkeypatch):
    """The save path is where a pair arrives, so it is where it gets proved — and an
    already-proved pair costs no call."""
    from src.config import account as account_mod
    from src.web.services import config_service

    monkeypatch.setattr(account_mod, "account_file_path", lambda s: tmp_path / "account.json")
    calls = []

    def probe(key_id, secret, base_url, timeout=0):
        calls.append(key_id)
        return {"ok": True, "message": "Credentials accepted", "account_number": "A1", "status": "ACTIVE"}

    monkeypatch.setattr(credentials, "probe", probe)
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")

    saved = config_service.update_account(
        {"ALPACA_PAPER_API_KEY": "PK-paper", "ALPACA_PAPER_API_SECRET": "PS-paper"}
    )
    assert saved["ok"] is True
    assert calls == ["PK-paper"], "a new pair is checked as it arrives"
    assert saved["verifications"]["paper"]["ok"] is True
    assert "PS-paper" not in json.dumps(saved)

    # A second save with the same pair must not call Alpaca again.
    again = config_service.update_account({"DATA_DIR": "data"})
    assert calls == ["PK-paper"]
    assert again["verifications"]["paper"]["checked"] is False
    assert again["verifications"]["paper"]["message"] == "Already verified"


def test_a_broken_verification_cannot_fail_a_save(tmp_path, monkeypatch):
    """The keys are already on disk by then: a network problem must not report the
    save as failed, or the operator would retype credentials that were fine. It must
    also not invent a verdict — no record means the trading gate still refuses."""
    from src.config import account as account_mod
    from src.web.services import config_service

    monkeypatch.setattr(account_mod, "account_file_path", lambda s: tmp_path / "account.json")
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")

    def explode(*a, **k):
        raise RuntimeError("network stack is on fire")

    monkeypatch.setattr(credentials, "probe", explode)
    saved = config_service.update_account(
        {"ALPACA_PAPER_API_KEY": "PK-paper", "ALPACA_PAPER_API_SECRET": "PS-paper"}
    )
    assert saved["ok"] is True
    assert (tmp_path / "account.json").exists()
    assert saved["verifications"] == {}, "no verdict may be invented when the check cannot run"

    # ...and the gate still refuses, which is the point of not inventing one.
    assert credentials.check_for(_s(tmp_path, **PAPER), "paper")["verified"] is False


# ---------------------------------------------------------------------------
# a rejected pair must not fail the save, and must not be stored either
# ---------------------------------------------------------------------------
def _save_ready(tmp_path, monkeypatch):
    """Point the account file at tmp and hand back the service."""
    from src.config import account as account_mod
    from src.web.services import config_service

    monkeypatch.setattr(account_mod, "account_file_path", lambda s: tmp_path / "account.json")
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    return config_service


def _probe_per_env(verdicts):
    """A probe that answers per key id, so one pair can pass while another is refused."""

    def probe(key_id, secret, base_url, timeout=0):
        outcome = verdicts.get(key_id, {"ok": True, "reason": "accepted", "message": "Credentials accepted"})
        return dict(outcome)

    return probe


def test_a_rejected_pair_does_not_fail_the_save(tmp_path, monkeypatch):
    """The reported bug: a bad live key pair reported the whole save as failed. The
    rest of the form is not the credential's problem."""
    config_service = _save_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        credentials, "probe",
        _probe_per_env({
            "PK-paper": {"ok": True, "reason": "accepted", "message": "Credentials accepted", "account_number": "PA1"},
            "LK-bogus": {"ok": False, "reason": "rejected", "message": "Alpaca rejected these credentials (401)"},
        }),
    )
    saved = config_service.update_account({
        "ALPACA_PAPER_API_KEY": "PK-paper", "ALPACA_PAPER_API_SECRET": "PS-paper",
        "ALPACA_LIVE_API_KEY": "LK-bogus", "ALPACA_LIVE_API_SECRET": "LS-bogus",
        "LIVE_LOOKBACK_DAYS": "25",
    })

    assert saved["ok"] is True, "only the pair fails, not the save"
    assert saved["errors"] == []
    assert "NOT saved" in saved["message"] and "LIVE" in saved["message"]
    assert set(saved["unsaved_pairs"]) == {"live"}
    assert "401" in saved["unsaved_pairs"]["live"]

    stored = json.loads((tmp_path / "account.json").read_text())["settings"]
    # The pair that works is kept; the one that does not is left behind entirely...
    assert stored["ALPACA_PAPER_API_KEY"] == "PK-paper"
    assert "ALPACA_LIVE_API_KEY" not in stored and "ALPACA_LIVE_API_SECRET" not in stored
    # ...and everything else in the form lands as normal.
    assert stored["LIVE_LOOKBACK_DAYS"] == "25"


def test_the_valid_pair_is_saved_even_when_the_other_one_is_rejected(tmp_path, monkeypatch):
    """The mirror image: paper is the bad one, live is good. The good pair must not be
    collateral damage of the bad one."""
    config_service = _save_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        credentials, "probe",
        _probe_per_env({
            "LK-live": {"ok": True, "reason": "accepted", "message": "Credentials accepted", "account_number": "LV1"},
            "PK-bogus": {"ok": False, "reason": "rejected", "message": "Alpaca rejected these credentials (401)"},
        }),
    )
    saved = config_service.update_account({
        "ALPACA_PAPER_API_KEY": "PK-bogus", "ALPACA_PAPER_API_SECRET": "PS-bogus",
        "ALPACA_LIVE_API_KEY": "LK-live", "ALPACA_LIVE_API_SECRET": "LS-live",
    })

    assert saved["ok"] is True
    assert set(saved["unsaved_pairs"]) == {"paper"}
    assert saved["verifications"]["live"]["ok"] is True
    stored = json.loads((tmp_path / "account.json").read_text())["settings"]
    assert stored["ALPACA_LIVE_API_KEY"] == "LK-live"
    assert "PK-bogus" not in stored.values()


def test_a_rejected_pair_does_not_overwrite_a_working_one(tmp_path, monkeypatch):
    """A pair that never worked must not replace one that might."""
    config_service = _save_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(credentials, "probe", _probe_per_env({}))  # everything passes
    config_service.update_account({
        "ALPACA_LIVE_API_KEY": "LK-good", "ALPACA_LIVE_API_SECRET": "LS-good",
    })

    monkeypatch.setattr(
        credentials, "probe",
        _probe_per_env({"LK-bad": {"ok": False, "reason": "rejected", "message": "Alpaca rejected these credentials (401)"}}),
    )
    saved = config_service.update_account({
        "ALPACA_LIVE_API_KEY": "LK-bad", "ALPACA_LIVE_API_SECRET": "LS-bad",
    })
    assert set(saved["unsaved_pairs"]) == {"live"}
    stored = json.loads((tmp_path / "account.json").read_text())["settings"]
    assert stored["ALPACA_LIVE_API_KEY"] == "LK-good", "the rejected pair was left out"
    assert stored["ALPACA_LIVE_API_SECRET"] == "LS-good"


def test_a_pair_that_could_not_be_reached_is_still_saved(tmp_path, monkeypatch):
    """"We could not ask" is not evidence. Discarding the operator's values because
    the network was down would look exactly like the save silently failing."""
    config_service = _save_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        credentials, "probe",
        _probe_per_env({"LK-live": {"ok": False, "reason": "unreachable", "message": "Could not reach https://api.alpaca.markets — timeout"}}),
    )
    saved = config_service.update_account({
        "ALPACA_LIVE_API_KEY": "LK-live", "ALPACA_LIVE_API_SECRET": "LS-live",
    })

    assert saved["ok"] is True and saved["unsaved_pairs"] == {}
    stored = json.loads((tmp_path / "account.json").read_text())["settings"]
    assert stored["ALPACA_LIVE_API_KEY"] == "LK-live", "unproven is not the same as wrong"
    assert saved["verifications"]["live"]["rejected"] is False
    assert saved["verifications"]["live"]["reason"] == "unreachable"


def test_an_unchanged_pair_is_not_checked_again_by_a_save(tmp_path, monkeypatch):
    """Every save would otherwise spend a round trip per pair re-asking a question
    that already has an answer."""
    config_service = _save_ready(tmp_path, monkeypatch)
    calls = []

    def probe(key_id, secret, base_url, timeout=0):
        calls.append(key_id)
        return {"ok": True, "reason": "accepted", "message": "Credentials accepted", "account_number": "A1"}

    monkeypatch.setattr(credentials, "probe", probe)
    pairs = {
        "ALPACA_PAPER_API_KEY": "PK-paper", "ALPACA_PAPER_API_SECRET": "PS-paper",
        "ALPACA_LIVE_API_KEY": "LK-live", "ALPACA_LIVE_API_SECRET": "LS-live",
    }
    config_service.update_account(pairs)
    assert calls == ["PK-paper", "LK-live"]

    # Same pairs, one unrelated field: no credential question is asked at all.
    again = config_service.update_account({**pairs, "LIVE_LOOKBACK_DAYS": "30"})
    assert calls == ["PK-paper", "LK-live"]
    assert again["verifications"]["paper"]["checked"] is False


def test_the_account_payload_exposes_the_verdicts():
    schema = client.get("/api/v1/account").json()
    assert set(schema) >= {"file", "file_exists", "groups", "credentials"}


def test_the_popup_reads_the_pair_the_bot_would_actually_use(tmp_path, monkeypatch):
    """The verdict must be about the STORED pair, not about ``.env`` alone.

    A bare ``Settings()`` does not see the account file, so it reported every stored
    key as unset — the popup said "no key set" for keys the bot was using.
    """
    from src.config import account as account_mod
    from src.web.services import config_service

    monkeypatch.setattr(account_mod, "account_file_path", lambda s: tmp_path / "account.json")
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    account_mod.save_account(Settings(_env_file=None), {"ALPACA_PAPER_API_KEY": "PK-paper", "ALPACA_PAPER_API_SECRET": "PS-paper"})
    from src.config.effective import invalidate

    invalidate()

    schema = config_service.get_account_schema()
    assert schema["credentials"]["paper"]["keys_set"] is True
    assert schema["credentials"]["live"]["keys_set"] is False
    # ...and the secret is not in the payload that reaches the browser.
    assert "PS-paper" not in json.dumps(schema)


# ---------------------------------------------------------------------------
# the UI wiring
# ---------------------------------------------------------------------------
def test_the_popup_renders_a_validate_button_and_a_verdict():
    js = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function credentialRow(" in js
    assert "f.verify" in js, "the row is placed from the schema, not a hard-coded key"
    # Validate checks the values IN THE FORM: answering "credentials not supplied"
    # about keys visibly sitting in the boxes was the bug.
    assert "spec.key_key" in js and "spec.secret_key" in js
    assert "/api/v1/account/verify" in js
    assert "async function validateCredentials(" in js
    # The row is built with NO verdict: a badge is the answer to a check, and no
    # check has been run yet. Painting the stored state here is what showed
    # "⚠ not valid" next to a box the operator had not filled in.
    assert "credentialRow(f.verify)" in js
    assert "renderCredentialState(" not in js, "stored verdicts must never be painted"
    assert "clearCredentialBadges();" in js, "opening the popup clears the last visit"
    # No verdict about the pair in play => no badge at all (not "not checked").
    assert "if (!c.has_verdict) {\n    clearCredentialBadge(badge);" in js
    assert "function credentialMessage(" in js
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".cred-row" in css and ".cred-badge" in css
    assert ".cred-badge.ok" in css and ".cred-badge.bad" in css
