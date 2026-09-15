"""Are the Alpaca credentials actually *working*, or merely present?

Having a key pair configured is not the same as being able to trade with it: keys
are copied from the wrong account page, revoked, or given the "paper" secret of a
live key. Nothing local can tell the difference — only the broker can — so this
module asks Alpaca itself, and remembers the answer.

Three rules shape it:

* **Verification is per environment, and ties to the key it verified.** The record
  is keyed by a fingerprint of the key ID, so swapping keys invalidates it without
  anyone having to remember to. Nothing secret is ever written: the fingerprint is
  a hash, and the secret is only ever sent to Alpaca, over TLS, and dropped.
* **A result is only cached when it is a PASS.** A failure may be a revoked key, but
  it may equally be a flaky network; caching "invalid" would turn a blip into a
  state the operator has to clear by hand. So a failed check is retried the next
  time it is asked for — and re-checking is always available explicitly.
* **This is not the configuration layer.** The verdict lives in
  ``data/credential_checks.json`` beside the datasets, because it is runtime state:
  the account file is exactly what the trading lock freezes, so a verdict stored
  there could not even be refreshed while trading is on.

The point of all of it: ``trading_service.turn_on`` refuses to start trading on an
environment whose credentials have not been verified. "Trading is ON" must never be
a promise the bot cannot keep.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.config import state_files
from src.execution.config import ENVIRONMENTS, LIVE_BASE_URL, PAPER_BASE_URL

logger = logging.getLogger(__name__)

STATE_FILENAME = "credential_checks.json"

# Long enough to notice, short enough that a hung broker cannot pin a request
# thread open indefinitely. Verifying is an interactive action.
VERIFY_TIMEOUT_SECONDS = 10.0

# ``GET /v2/account`` is the cheapest call that proves BOTH halves of "working":
# it authenticates the key pair AND confirms we reach the right endpoint.
ACCOUNT_PATH = "/v2/account"


class CredentialError(RuntimeError):
    """Verification could not be completed (network, timeout, unexpected reply)."""


def _text(value) -> str:
    """A settings value as a trimmed string (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(env: str, key_id: str) -> str:
    """A stable, non-reversible id for one key pair.

    Stored instead of the key ID so a runtime file never holds even the "username"
    half of a credential, and so a swapped key changes the fingerprint — which is
    what makes an old verdict expire on its own.
    """
    raw = f"{env}:{key_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def state_path(settings):
    """``<data root>/credential_checks.json`` — beside ``trading.json``."""
    return state_files.state_path(settings, STATE_FILENAME)


def _read(settings) -> Dict[str, Any]:
    return state_files.read_json(state_path(settings), {})


def _write(settings, data: Dict[str, Any]) -> None:
    state_files.write_json(state_path(settings), data)


def keys_for(settings, env: str) -> Dict[str, str]:
    """``{key_id, secret, base_url}`` for an environment, from the settings."""
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment: {env!r}")
    if env == "live":
        return {
            "key_id": _text(settings.alpaca_live_api_key),
            "secret": _text(settings.alpaca_live_api_secret),
            "base_url": LIVE_BASE_URL,
        }
    return {
        "key_id": _text(settings.alpaca_paper_api_key),
        "secret": _text(settings.alpaca_paper_api_secret),
        "base_url": PAPER_BASE_URL,
    }


def probe(key_id: str, secret: str, base_url: str, *, timeout: float = VERIFY_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Ask Alpaca whether this key pair works. Never raises.

    Returns ``{ok, message, reason, account_number?, status?}``. The reply deliberately
    carries no credential: the account number and status are enough to show the
    operator that the RIGHT account answered.

    ``reason`` is what lets a caller tell the two kinds of failure apart, because they
    mean different things:

    * ``rejected`` — Alpaca answered 401/403. This pair cannot work, now or later.
    * ``unreachable`` — nothing answered. Unknown, not wrong; a flaky network must not
      be recorded as a verdict about the credential.
    * ``unexpected`` — something answered, but not an answer we understand.

    Saving a pair follows that distinction (see ``config_service.update_account``): a
    rejected pair is not written, an unverifiable one is written unproven.
    """
    import requests  # imported here so the module stays importable without it

    url = f"{base_url}{ACCOUNT_PATH}"
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "cannot verify"
        # The message can name the host but never the credential, and requests'
        # own error text is passed through only after scrubbing the secret.
        detail = str(exc).replace(secret, "***") if secret else str(exc)
        return {"ok": False, "reason": "unreachable", "message": f"Could not reach {base_url} — {detail}"}

    if resp.status_code == 200:
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        account = str(body.get("account_number") or "")
        status = str(body.get("status") or "")
        return {
            "ok": True,
            "reason": "accepted",
            "message": "Credentials accepted" + (f" — account {account} ({status})" if account else ""),
            "account_number": account,
            "status": status,
        }
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "reason": "rejected",
            "message": (
                f"Alpaca rejected these credentials ({resp.status_code}). Check that the key "
                "belongs to this account type — paper and live keys are different."
            ),
        }
    return {"ok": False, "reason": "unexpected", "message": f"Alpaca answered {resp.status_code} for {url}"}


def check_for(settings, env: str) -> Dict[str, Any]:
    """The stored verdict for an environment, against its CURRENT key.

    ``verified`` is True only when a passing check exists for the fingerprint of
    the key configured right now — so editing the key silently expires the verdict.
    ``has_verdict`` says whether there is a verdict about THIS pair at all, which is
    what the popup shows the badge for: a pair nobody has checked yet has nothing to
    report, and reporting "not checked"/"no key set" against it was noise the
    operator should not have to read past. Never raises.
    """
    keys = keys_for(settings, env)
    record = (_read(settings).get("environments") or {}).get(env) or {}
    fp = fingerprint(env, keys["key_id"]) if keys["key_id"] else ""
    recorded_fp = str(record.get("fingerprint") or "")
    matches = bool(fp) and recorded_fp == fp
    return {
        "env": env,
        "keys_set": bool(keys["key_id"] and keys["secret"]),
        "has_verdict": matches,
        "verified": matches and bool(record.get("ok")),
        "fingerprint_matches": matches,
        "ok": bool(record.get("ok")),
        "checked_at": record.get("checked_at") if matches else None,
        "message": record.get("message") if matches else "",
        "account_number": record.get("account_number") if matches else "",
        "reason": record.get("reason") if matches else "",
        "stale": bool(record) and not matches,
    }


def all_checks(settings) -> Dict[str, Dict[str, Any]]:
    """The verdict for every environment — what the Account popup renders."""
    return {env: check_for(settings, env) for env in ENVIRONMENTS}


def _nothing_to_check(env: str, message: str) -> Dict[str, Any]:
    """The answer to a check that had nothing to look at.

    Deliberately shapeless as a verdict: ``checked`` and ``has_verdict`` are both
    False, so the UI reports the message and leaves the row alone instead of
    labelling a pair that was never examined.
    """
    return {
        "env": env,
        "ok": False,
        "verified": False,
        "keys_set": False,
        "checked": False,
        "saved": False,
        "has_verdict": False,
        "message": message,
    }


def verify(
    settings,
    env: str,
    *,
    force: bool = False,
    key_id: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify one environment's credentials and record the verdict.

    ``key_id``/``secret`` are the values someone is *looking at* — the ones typed
    into the Account popup. Supplying them (even as empty strings) means "check the
    form, not the file": the Validate button must never answer about a pair the
    operator cannot see, and it must never fall back to a stored credential when the
    boxes are empty, because pressing Validate on an empty form would then report a
    verdict about a key nobody asked about. An empty form is answered with "nothing
    to validate".

    Passing neither argument checks the STORED pair instead, which is what the
    save-time check and turning trading on need: they care about the pair the bot
    would actually trade with.

    Either way the verdict is recorded under the fingerprint of the pair that was
    really checked, so saving the same values afterwards opens the gate without a
    second call.

    ``force=False`` (the save path, and turning trading on) skips a pair that already
    has a PASSING record for the same key: re-checking a known-good pair would be a
    pointless call to Alpaca, and the operator asked for exactly that economy.
    ``force=True`` (the Validate button) always asks.
    """
    stored = keys_for(settings, env)
    if key_id is None and secret is None:
        keys = dict(stored)  # the pair the bot would trade with
    else:
        keys = {
            "key_id": _text(key_id),
            "secret": _text(secret),
            "base_url": stored["base_url"],
        }
    stored_match = keys["key_id"] == stored["key_id"] and keys["secret"] == stored["secret"]
    if not keys["key_id"] and not keys["secret"]:
        return _nothing_to_check(
            env, f"No {env.upper()} credentials found to validate — enter the API key and its secret."
        )
    if not keys["key_id"] or not keys["secret"]:
        return _nothing_to_check(
            env, f"Both the {env.upper()} API key and its secret are needed before they can be verified."
        )
    # A check of what is already stored, and already passing, needs no call.
    if not force and stored_match and check_for(settings, env)["verified"]:
        return {**check_for(settings, env), "checked": False, "saved": True, "message": "Already verified"}

    result = probe(keys["key_id"], keys["secret"], keys["base_url"])
    record = {
        "fingerprint": fingerprint(env, keys["key_id"]),
        "ok": bool(result["ok"]),
        "reason": result.get("reason", ""),
        "checked_at": _now_iso(),
        "message": result["message"],
        "account_number": result.get("account_number", ""),
    }
    data = _read(settings)
    data.setdefault("environments", {})[env] = record
    _write(settings, data)
    if not result["ok"]:
        logger.warning("Credential check FAILED for %s: %s", env.upper(), result["message"])
    else:
        logger.info("Credential check passed for %s (account %s)", env.upper(), record["account_number"] or "?")
    return {
        "env": env,
        "ok": bool(result["ok"]),
        "verified": bool(result["ok"]),
        "keys_set": True,
        "checked": True,
        # Just checked, so there IS a verdict about this pair — the UI's cue to show
        # the badge. Without it a fresh result would be reported and then hidden.
        "has_verdict": True,
        # ``rejected`` means the broker said no (so this pair cannot be stored),
        # ``unreachable``/``unexpected`` only mean we do not know yet.
        "reason": result.get("reason", ""),
        "rejected": result.get("reason") == "rejected",
        # False when the checked pair is NOT the stored one: it is valid, but it is
        # not what the bot would trade with until the form is saved.
        "saved": stored_match,
        "message": result["message"],
        "account_number": record["account_number"],
        "checked_at": record["checked_at"],
    }


def verify_new(settings, skip_envs=()) -> Dict[str, Dict[str, Any]]:
    """Verify every environment that has keys but no passing verdict yet.

    Called after an account save, so a freshly added pair is checked at the moment
    it is added, and an unchanged pair costs nothing. ``skip_envs`` names the
    environments the caller has already checked itself, so the save does not ask
    Alpaca the same question twice.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for env in ENVIRONMENTS:
        if env in skip_envs:
            continue
        keys = keys_for(settings, env)
        if not keys["key_id"] and not keys["secret"]:
            continue
        state = check_for(settings, env)
        if state["verified"]:
            out[env] = {**state, "checked": False, "message": "Already verified"}
            continue
        out[env] = verify(settings, env, force=True)
    return out


def require_verified(settings, env: str) -> Optional[str]:
    """The reason trading may not start on ``env``, or None when it may.

    Returns a message rather than raising, because the caller's job is to explain
    the refusal to an operator, not to fail a request.
    """
    keys = keys_for(settings, env)
    if not keys["key_id"] or not keys["secret"]:
        return f"The Alpaca {env.upper()} API key and secret are not set."
    state = check_for(settings, env)
    if state["verified"]:
        return None
    if state["stale"]:
        return (
            f"The Alpaca {env.upper()} credentials changed since they were verified — "
            "verify them again in Account Settings before trading."
        )
    if state["checked_at"]:
        return (
            f"The Alpaca {env.upper()} credentials FAILED verification ({state['message']}). "
            "Fix them in Account Settings, then verify again."
        )
    return (
        f"The Alpaca {env.upper()} credentials have not been verified yet. "
        "Open Account Settings and press Validate next to them."
    )
