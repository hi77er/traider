"""The portal's lock: one person, one PIN, and a signed cookie that goes stale.

Framework-free on purpose. The middleware, the routes and the CLI all call in here, and the tests
call it directly, so nothing below knows what HTTP is.

Two rules the rest of the system depends on:

* **This is web-app state, and only that.** It never reads or writes ``trading.json`` — a lockout, a
  sign-out or a PIN change must not be able to stop, start or alter a trading run. The loop is a
  separate process that reads files and does not know this module exists.
* **"Enabled" means the store exists.** No ``data/auth.json``, no enforcement: a fresh checkout and
  the whole test suite behave exactly as before until someone deliberately creates it, with the CLI.

The PIN is never stored or logged. What is stored is scrypt over it with a per-install salt, with
the parameters on the record so the cost can be raised later without a migration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import loop_state, state_files

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_FILENAME",
    "DEFAULT_ABSOLUTE_SECONDS",
    "DEFAULT_IDLE_SECONDS",
    "KDF",
    "LOCKOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_PIN_DIGITS",
    "MIN_PIN_DIGITS",
    "change_pin",
    "create",
    "describe",
    "disable",
    "enabled",
    "issue",
    "load",
    "machine_ok",
    "pin_problem",
    "read",
    "reset",
    "sign_out_others",
    "store_path",
    "verify",
]

#: Beside ``trading.json`` and ``loop.lock`` — runtime state, never in git (the root-anchored
#: ``/data/`` ignore rule covers it), and never the same file as the trading switch.
AUTH_FILENAME = "auth.json"

#: How the PIN is stretched. PBKDF2-HMAC-SHA256 rather than scrypt, and not by preference: this
#: project runs the system Python 3.9.6, which links LibreSSL 2.8.3 — ``hashlib.scrypt`` does not
#: exist there. 600,000 iterations measures ~145 ms on the laptop that runs the portal, which is a
#: real cost per guess and nothing anyone notices per login. The parameters ride on the record, so
#: an install on a host with OpenSSL can move to scrypt without a migration.
KDF: Dict[str, Any] = {"algo": "pbkdf2_sha256", "iterations": 600_000, "dklen": 32}

#: Five wrong PINs, then five minutes. Both PERSISTED: a restart must not hand an attacker a fresh
#: budget, and the counter is what makes a four-digit PIN a reasonable thing to type.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

#: How long a session may sit unused, and the hard cap on one however busy it is. The browser's own
#: inactivity lock uses the first of these; see ``docs/portal-auth.md``.
DEFAULT_IDLE_SECONDS = 900
DEFAULT_ABSOLUTE_SECONDS = 86400

#: One user for now. The record is a LIST keyed by this id and the session carries ``sub``, which is
#: the whole of the "more than one account later" seam.
DEFAULT_USER_ID = "owner"

#: What a PIN may be. Here rather than in the CLI because three callers now write one — the CLI, the
#: lock screen's create flow and its change flow — and a rule that lives in one of them is a rule
#: the other two do not have.
MIN_PIN_DIGITS = 4
MAX_PIN_DIGITS = 12

#: The shapes everyone types first. Not a policy, a nudge: this door faces whatever can reach the
#: port, and the length rule alone accepts a PIN whose whole value is that it is easy to guess. The
#: same-digit case is handled by rule rather than by listing 0000 through 9999.
WEAK_PINS = ("1234", "123456", "12345678", "87654321")


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def store_path(settings) -> Path:
    """``<data root>/auth.json`` — the presence of which is the whole on/off switch."""
    return state_files.state_path(settings, AUTH_FILENAME)


def load(settings) -> Optional[Dict[str, Any]]:
    """The store, or ``None`` when there is none to read. Never raises on a broken file."""
    path = store_path(settings)
    if not path.exists():
        return None
    record = state_files.read_json(path, default={})
    return record or None


def save(settings, record: Dict[str, Any]) -> None:
    state_files.write_json(store_path(settings), record)


def enabled(settings) -> bool:
    """Is the lock on? — the store exists AND has somebody in it."""
    record = load(settings)
    return bool(record and record.get("users"))


def idle_seconds(settings, record: Optional[Dict[str, Any]] = None) -> int:
    """The idle window, in seconds: the SETTING wins, then the store, then the default.

    One reader, so the three places that care cannot disagree: the middleware refusing a
    stale session, the login that reports the window to the browser, and ``status`` which is
    what the security card prints. ``record`` is passed in where the caller already has it.

    ``AUTH_IDLE_MINUTES`` is read from the ACCOUNT file, and that is deliberate rather than
    taking ``settings.auth_idle_minutes``. The object the auth paths are handed is
    ``get_settings()`` — the bare, cached ``.env`` settings, which know nothing of the account
    layer — and the effective ones are resolved PER STRATEGY, which this module must not touch:
    the lock belongs to the machine, and a PIN must not depend on which strategy is active.
    Reading the file the popup writes is the one way to get the operator's number.
    """
    minutes = _configured_minutes(settings)
    if minutes:
        return minutes * 60
    if record is None:
        record = load(settings)
    return int((record or {}).get("idle_seconds") or DEFAULT_IDLE_SECONDS)


def _configured_minutes(settings) -> int:
    """``AUTH_IDLE_MINUTES`` as a usable number of minutes, or ``0`` when there is none.

    Bounded here as well as in the model: the form refuses anything outside the range, but a
    hand-edited ``account.json`` should not be able to leave the portal open for a day either.
    A file that cannot be read is not a reason to fail a request — the lock falls back to the
    window it has always had.
    """
    from src.config import account as account_mod  # noqa: PLC0415 - one small read, only here
    from src.config.settings import (  # noqa: PLC0415
        AUTH_IDLE_MINUTES_MAX,
        AUTH_IDLE_MINUTES_MIN,
    )

    try:
        stored = account_mod.account_values(settings).get("AUTH_IDLE_MINUTES")
    except Exception:  # noqa: BLE001 - a broken account file must not break the lock
        logger.warning("Could not read AUTH_IDLE_MINUTES — keeping the window already in force")
        return 0
    try:
        minutes = int(str(stored).strip())
    except (TypeError, ValueError):
        return 0
    if not AUTH_IDLE_MINUTES_MIN <= minutes <= AUTH_IDLE_MINUTES_MAX:
        logger.warning(
            "AUTH_IDLE_MINUTES is %s, outside %s-%s — ignoring it",
            minutes, AUTH_IDLE_MINUTES_MIN, AUTH_IDLE_MINUTES_MAX,
        )
        return 0
    return minutes


def disable(settings) -> bool:
    """Turn the lock off by removing the store. Returns whether there was one."""
    path = store_path(settings)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    logger.warning("Auth disabled: removed %s — the portal is open again", path)
    return True


def _user(record: Optional[Dict[str, Any]], user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for user in (record or {}).get("users") or []:
        if not isinstance(user, dict):
            continue
        if user_id is None or user.get("id") == user_id:
            return user
    return None


def pin_problem(pin: str) -> Optional[str]:
    """Why this PIN will not do, or ``None``. Pure, so the CLI, the routes and the page agree.

    The page mirrors these three rules in JavaScript for instant feedback, which is why they are
    worth keeping few: every one of them exists in two places, and the server's copy is the one
    that decides.
    """
    text = str(pin or "").strip()
    if not text.isdigit():
        return "a PIN is digits only"
    if not MIN_PIN_DIGITS <= len(text) <= MAX_PIN_DIGITS:
        return f"a PIN is {MIN_PIN_DIGITS}-{MAX_PIN_DIGITS} digits"
    if len(set(text)) == 1:
        return "that is one digit repeated — pick something else"
    if text in WEAK_PINS:
        return f"{text} is the first PIN anyone tries"
    return None


def _hash(pin: str, salt: str, kdf: Optional[Dict[str, Any]]) -> str:
    """The stored form of a PIN. The algorithm is named on the record, so it can be changed."""
    params = {**KDF, **(kdf or {})}
    algo = str(params.get("algo") or KDF["algo"])
    raw = bytes.fromhex(str(salt))
    if algo == "scrypt":
        scrypt = getattr(hashlib, "scrypt", None)
        if scrypt is None:  # pragma: no cover - this interpreter, and the reason KDF is PBKDF2
            raise RuntimeError(
                "this Python's OpenSSL has no scrypt (LibreSSL) — the record asks for scrypt"
            )
        return scrypt(
            str(pin).encode("utf-8"), salt=raw, n=int(params["n"]), r=int(params["r"]),
            p=int(params["p"]), dklen=int(params["dklen"]),
        ).hex()
    if algo != "pbkdf2_sha256":
        raise ValueError(f"unknown PIN hash {algo!r}")
    return hashlib.pbkdf2_hmac(
        "sha256", str(pin).encode("utf-8"), raw, int(params["iterations"]),
        dklen=int(params["dklen"]),
    ).hex()


def _fresh_user(user_id: str, pin: str, label: str, *, at: datetime) -> Dict[str, Any]:
    salt = secrets.token_hex(16)
    return {
        "id": user_id,
        "label": str(label or ""),
        "salt": salt,
        "hash": _hash(pin, salt, KDF),
        "kdf": dict(KDF),
        "generation": 1,
        "failed_attempts": 0,
        "locked_until": None,
        "updated_at": loop_state.stamp(at),
    }


def create(
    settings,
    pin: str,
    *,
    label: str = "",
    user_id: str = DEFAULT_USER_ID,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
    absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write a fresh store with one user. Refuses a store that already exists.

    Refusing rather than overwriting is the whole safety story of the CLI: ``set`` cannot silently
    reset somebody's PIN, and ``reset`` is the verb that says out loud that it can. That refusal
    comes first, so a store that exists is reported as such whatever PIN was offered with it.
    """
    if enabled(settings):
        raise FileExistsError(f"{store_path(settings)} already exists — use 'change' or 'reset'")
    problem = pin_problem(pin)
    if problem:
        return {"ok": False, "enabled": False, "reason": problem}
    moment = at or loop_state.now()
    record = {
        "version": 1,
        "secret": secrets.token_hex(32),
        "machine_token": secrets.token_hex(32),
        "idle_seconds": int(idle_seconds),
        "absolute_seconds": int(absolute_seconds),
        "users": [_fresh_user(user_id, pin, label, at=moment)],
        "created_at": loop_state.stamp(moment),
    }
    save(settings, record)
    logger.warning("Auth enabled for %r — the portal now asks for a PIN", user_id)
    return {"ok": True, **describe(settings)}


def _lock_until(moment: datetime) -> datetime:
    return moment + timedelta(seconds=LOCKOUT_SECONDS)


def verify(settings, pin: str, *, at: Optional[datetime] = None) -> Dict[str, Any]:
    """Check a PIN, and count the failures that make a four-digit one defensible.

    The lockout is written to the store, so it survives a restart and applies to every request that
    guesses — including the change-PIN endpoint, which asks for the current PIN and is therefore the
    same secret being guessed from a second door.
    """
    moment = at or loop_state.now()
    record = load(settings)
    user = _user(record)
    if user is None:
        return {"ok": False, "enabled": False, "reason": "no PIN is set for this portal"}

    until = loop_state.parse_stamp(user.get("locked_until"))
    if until is not None and moment < until:
        left = int((until - moment).total_seconds()) + 1
        return {
            "ok": False, "enabled": True, "locked": True, "user": user.get("id"),
            "locked_until": user.get("locked_until"),
            "reason": f"too many wrong PINs — try again in {left // 60 + 1} minute(s)",
        }

    if hmac.compare_digest(_hash(pin, user["salt"], user.get("kdf")), str(user.get("hash") or "")):
        if int(user.get("failed_attempts") or 0) or user.get("locked_until"):
            user["failed_attempts"] = 0
            user["locked_until"] = None
            save(settings, record)
        return {
            "ok": True, "enabled": True, "user": user.get("id"),
            "generation": int(user.get("generation") or 1),
            "idle_seconds": idle_seconds(settings, record),
            "absolute_seconds": int(
                (record or {}).get("absolute_seconds") or DEFAULT_ABSOLUTE_SECONDS
            ),
        }

    attempts = int(user.get("failed_attempts") or 0) + 1
    result: Dict[str, Any] = {"ok": False, "enabled": True, "user": user.get("id")}
    if attempts >= MAX_ATTEMPTS:
        user["failed_attempts"] = 0
        user["locked_until"] = loop_state.stamp(_lock_until(moment))
        result.update(
            locked=True,
            locked_until=user["locked_until"],
            reason=f"too many wrong PINs — locked for {LOCKOUT_SECONDS // 60} minute(s)",
        )
    else:
        user["failed_attempts"] = attempts
        left = MAX_ATTEMPTS - attempts
        result.update(reason=f"wrong PIN — {left} attempt(s) left")
    save(settings, record)
    logger.warning("A wrong PIN was tried (%s of %s)", attempts, MAX_ATTEMPTS)
    return result


def _rotate_sessions(
    settings, record: Dict[str, Any], user: Dict[str, Any], moment: datetime, *, why: str
) -> None:
    """Invalidate every cookie that exists: a new generation, and a new signing secret.

    Both, because neither alone is enough — the generation alone would be undone by a restored
    record, and the secret alone would leave every payload naming a generation that still matches.
    """
    user["generation"] = int(user.get("generation") or 1) + 1
    user["failed_attempts"] = 0
    user["locked_until"] = None
    user["updated_at"] = loop_state.stamp(moment)
    record["secret"] = secrets.token_hex(32)
    save(settings, record)
    logger.warning("%s — every session signed with the old secret is refused", why)


def _rewrite_pin(
    settings, record: Dict[str, Any], user: Dict[str, Any], pin: str, moment: datetime
) -> Dict[str, Any]:
    """Re-hash in place, and invalidate every session that exists."""
    user["salt"] = secrets.token_hex(16)
    user["hash"] = _hash(pin, user["salt"], KDF)
    user["kdf"] = dict(KDF)
    _rotate_sessions(settings, record, user, moment, why="The portal's PIN was changed")
    return describe(settings)


def change_pin(
    settings, current: str, new: str, *, at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Change the PIN, given the current one — which is also why a stolen cookie cannot.

    A wrong current PIN is answered by the same counter the login form feeds, so this is not a
    second door to guess one secret through. Choosing the PIN you already have is not an error and
    rotates nothing: there is nothing to change, and signing every session out over it would be a
    surprise.
    """
    moment = at or loop_state.now()
    checked = verify(settings, current, at=moment)
    if not checked.get("ok"):
        return {
            "ok": False,
            "locked": bool(checked.get("locked")),
            "locked_until": checked.get("locked_until"),
            "reason": checked.get("reason") or "the current PIN was refused",
        }
    record = load(settings)
    user = _user(record)
    if user is None:
        return {"ok": False, "reason": "no PIN is set for this portal"}
    problem = pin_problem(new)
    if problem:
        return {"ok": False, "reason": problem}
    if hmac.compare_digest(_hash(new, user["salt"], user.get("kdf")), str(user.get("hash") or "")):
        return {"ok": True, "unchanged": True, **describe(settings)}
    return {"ok": True, "unchanged": False, **(_rewrite_pin(settings, record, user, new, moment))}


def sign_out_others(settings, *, at: Optional[datetime] = None) -> Dict[str, Any]:
    """Keep the PIN, expire every session; the caller hands itself a fresh cookie afterwards.

    For the session the header's own "Sign out" cannot reach: another browser, another tab, another
    machine, or one somebody left open.
    """
    moment = at or loop_state.now()
    record = load(settings)
    user = _user(record)
    if user is None:
        return {"ok": False, "reason": "no PIN is set for this portal"}
    _rotate_sessions(settings, record, user, moment, why="Signed out everywhere")
    return {"ok": True, **describe(settings)}


def reset(settings, pin: str, *, at: Optional[datetime] = None) -> Dict[str, Any]:
    """Set a new PIN WITHOUT the old one. Local-machine access is the credential.

    This is the forgotten-PIN path, and the only one: no email, no second account, no recovery code.
    It sits beside ``data/credentials.json``, which is the same trust level — whoever can run this
    already has the broker keys.
    """
    moment = at or loop_state.now()
    problem = pin_problem(pin)
    if problem:
        return {"ok": False, "reason": problem}
    record = load(settings)
    user = _user(record)
    if user is None:
        # Nothing to reset: the first PIN is the same thing, and the caller gets the same answer.
        return create(settings, pin, at=moment)
    return {"ok": True, **(_rewrite_pin(settings, record, user, pin, moment))}


# ---------------------------------------------------------------------------
# the session cookie
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(record: Dict[str, Any], payload: Dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(
        str(record.get("secret") or "").encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64(mac)}"


def issue(settings, user_id: str = DEFAULT_USER_ID, *, at: Optional[datetime] = None) -> Optional[str]:
    """A fresh cookie value for a user, or ``None`` when there is no such user."""
    record = load(settings)
    user = _user(record, user_id)
    if user is None:
        return None
    moment = at or loop_state.now()
    stamp = loop_state.stamp(moment)
    return _sign(
        record,
        {"sub": user.get("id"), "gen": int(user.get("generation") or 1), "iat": stamp, "seen": stamp},
    )


def read(settings, token: Optional[str], *, at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The session a cookie value stands for, or ``None``.

    ``None`` covers every way a session ends: a forged or truncated value, a secret that has since
    been rotated, a generation the user has moved past, and the two clocks — the idle window and the
    absolute cap. A caller that gets ``None`` has exactly one thing to do, which is ask for the PIN.
    """
    if not token or "." not in token:
        return None
    record = load(settings)
    if not record:
        return None
    body, _, mac = token.partition(".")
    expected = hmac.new(
        str(record.get("secret") or "").encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    user = _user(record, str(payload.get("sub") or ""))
    if user is None or int(payload.get("gen") or 0) != int(user.get("generation") or 1):
        return None

    moment = at or loop_state.now()
    seen = loop_state.parse_stamp(payload.get("seen"))
    issued = loop_state.parse_stamp(payload.get("iat"))
    if seen is None or issued is None:
        return None
    idle = idle_seconds(settings, record)
    absolute = int(record.get("absolute_seconds") or DEFAULT_ABSOLUTE_SECONDS)
    if (moment - seen).total_seconds() > idle:
        return None
    if (moment - issued).total_seconds() > absolute:
        return None
    return payload


def touch(settings, token: Optional[str], *, at: Optional[datetime] = None) -> Optional[str]:
    """Re-issue the same session with ``seen`` moved to now — the sliding half of the idle window.

    Returns ``None`` for a session that is no longer valid, so a caller can treat "cannot slide" and
    "not signed in" as the same answer.
    """
    payload = read(settings, token, at=at)
    if payload is None:
        return None
    record = load(settings)
    if record is None:
        return None
    moved = dict(payload)
    moved["seen"] = loop_state.stamp(at or loop_state.now())
    return _sign(record, moved)


def machine_ok(settings, presented: Optional[str]) -> bool:
    """Does a presented machine token match? Never raises, never logs the value."""
    record = load(settings) or {}
    expected = str(record.get("machine_token") or "")
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(presented), expected)


def describe(settings) -> Dict[str, Any]:
    """What the CLI's ``status`` prints — and never the hash, the salt, or a secret."""
    record = load(settings)
    user = _user(record)
    return {
        "enabled": enabled(settings),
        "path": str(store_path(settings)),
        "user": (user or {}).get("id"),
        "label": (user or {}).get("label") or "",
        "generation": int((user or {}).get("generation") or 0) or None,
        "updated_at": (user or {}).get("updated_at"),
        "locked_until": (user or {}).get("locked_until"),
        "failed_attempts": int((user or {}).get("failed_attempts") or 0),
        "idle_seconds": idle_seconds(settings, record),
        "absolute_seconds": int((record or {}).get("absolute_seconds") or DEFAULT_ABSOLUTE_SECONDS),
        "machine_token": bool((record or {}).get("machine_token")),
    }
