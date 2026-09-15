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
    assert "No LIVE API key or secret to verify" in got["message"]


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
    settings = _s(tmp_path, **PAPER)
    app.dependency_overrides[get_effective_settings_dep] = lambda: settings
    try:
        monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": True, "message": "Credentials accepted", "account_number": "A1", "status": "ACTIVE"})
        good = client.post("/api/v1/account/verify", json={"env": "paper"}).json()
        assert good["ok"] is True and good["result"]["verified"] is True
        assert good["credentials"]["paper"]["verified"] is True

        monkeypatch.setattr(credentials, "probe", lambda *a, **k: {"ok": False, "message": "Alpaca rejected these credentials (401)"})
        bad = client.post("/api/v1/account/verify", json={"env": "paper"}).json()
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


def test_a_masked_box_validates_the_stored_pair_not_the_mask(tmp_path, monkeypatch):
    """A re-opened popup shows "********" instead of the secret, so pressing Validate
    without touching the form must check what is stored — not the asterisks."""
    from src.web.services import config_service

    settings = _s(tmp_path, alpaca_live_api_key="LK-saved", alpaca_live_api_secret="LS-saved")
    monkeypatch.setattr(credentials, "state_path", lambda s: tmp_path / "credential_checks.json")
    monkeypatch.setattr(config_service, "get_effective_settings", lambda: settings)
    seen = []

    def fake_probe(key_id, secret, url, **kw):
        seen.append((key_id, secret))
        return {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"}

    monkeypatch.setattr(credentials, "probe", fake_probe)
    got = config_service.verify_credentials("live", "********", "********")
    assert got["ok"] is True
    assert seen == [("LK-saved", "LS-saved")], "the mask is not a credential"


def test_a_blank_field_falls_back_to_the_stored_value(tmp_path, monkeypatch):
    """Blank means "unchanged", exactly like a save — so validating a form where only
    the key was retyped must check the retyped key against the stored secret."""
    settings = _s(tmp_path, alpaca_live_api_key="LK-saved", alpaca_live_api_secret="LS-saved")

    def fake_probe(key_id, secret, url, **kw):
        assert (key_id, secret) == ("LK-new", "LS-saved"), "the form's key, the stored secret"
        return {"ok": True, "message": "ok", "account_number": "A1", "status": "ACTIVE"}

    monkeypatch.setattr(credentials, "probe", fake_probe)
    got = credentials.verify(settings, "live", force=True, key_id="LK-new", secret="")
    assert got["ok"] is True and got["saved"] is False

    monkeypatch.setattr(
        credentials, "probe",
        lambda key_id, secret, url, **k: {"ok": True, "message": "ok", "account_number": "A1"},
    )
    assert credentials.verify(settings, "live", force=True, key_id="", secret="")["ok"] is True


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
    assert "renderCredentialState(" in js
    # No verdict about the pair in play => no badge at all (not "not checked").
    assert "badge.hidden = !c.has_verdict" in js
    assert "function credentialMessage(" in js
    css = (ROOT / "src" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".cred-row" in css and ".cred-badge" in css
    assert ".cred-badge.ok" in css and ".cred-badge.bad" in css
