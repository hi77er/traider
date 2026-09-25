"""The portal's PIN, from the machine that runs it:

    .venv/bin/python -m src.web.auth set       # write the first PIN
    .venv/bin/python -m src.web.auth status    # what is set, and since when
    .venv/bin/python -m src.web.auth change    # change it, and sign out everywhere else
    .venv/bin/python -m src.web.auth reset     # a new PIN WITHOUT the old one — forgotten it
    .venv/bin/python -m src.web.auth disable   # delete the store; the portal is open again

The lock is OFF until ``set`` is run: there is no store, so nothing is enforced. It is ON from the
very next request — the middleware reads the store per request, so no restart is needed. An open
tab needs no special handling either: its next poll answers 401 and the page raises the lock itself.

Deliberately a local-machine tool. Whoever can run it already has ``data/credentials.json`` beside
it, so it is not a new way in — and it is the only recovery path there is, because the alternative
would be email or a second account, and this portal has neither.
"""

from __future__ import annotations

import json
import sys
from typing import List, Optional

from src.config.settings import get_settings
from src.web.services import auth_service

__all__ = ["VERBS", "main"]

VERBS = ("set", "change", "reset", "disable", "status")

#: What a PIN is, from the service — the lock screen writes them too, and one rule is the point.
MIN_DIGITS = auth_service.MIN_PIN_DIGITS
MAX_DIGITS = auth_service.MAX_PIN_DIGITS

USAGE = (
    "usage: python -m src.web.auth <verb>\n\n"
    "  set      write the first PIN (refuses if one is already set)\n"
    "  status   what is set, when it was last changed, who is locked out\n"
    "  change   change the PIN, given the current one — signs out every other session\n"
    "  reset    set a new PIN without the current one (local access is the credential)\n"
    "  disable  delete the store: the portal stops asking\n"
)


def _read_secret(prompt: str) -> str:
    """One prompt, read without echo. A named door, so a test can drive the verbs."""
    import getpass  # noqa: PLC0415 - only needed when a verb actually asks for something

    return getpass.getpass(prompt)


def _offer(prompt: str) -> Optional[str]:
    """Read a new PIN twice and check it. ``None`` means the operator's answer was not usable."""
    pin = str(_read_secret(prompt)).strip()
    problem = auth_service.pin_problem(pin)
    if problem:
        print(problem, file=sys.stderr)
        return None
    if pin != str(_read_secret("Repeat it: ")).strip():
        print("the two did not match — nothing was changed", file=sys.stderr)
        return None
    return pin


def _report(result: dict) -> int:
    if not result.get("ok"):
        print(f"refused — {result.get('reason') or 'no reason given'}", file=sys.stderr)
        return 1
    print("done.")
    print(json.dumps(_public(result), indent=2, sort_keys=True))
    return 0


def _public(result: dict) -> dict:
    """The parts worth printing. Never a hash, a salt or a secret."""
    keys = ("enabled", "user", "label", "generation", "updated_at", "locked_until",
            "failed_attempts", "idle_seconds", "absolute_seconds", "machine_token", "path")
    return {key: result[key] for key in keys if key in result}


def _status(settings) -> int:
    print(json.dumps(_public(auth_service.describe(settings)), indent=2, sort_keys=True))
    if not auth_service.enabled(settings):
        print("\nThe lock is OFF — no PIN is set, so nothing is enforced.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    verb = args[0]
    if verb not in VERBS:
        print(f"unknown verb {verb!r}\n\n{USAGE}", file=sys.stderr)
        return 2

    settings = get_settings()

    if verb == "status":
        return _status(settings)

    if verb == "disable":
        removed = auth_service.disable(settings)
        print("the lock is off — the portal is open again" if removed
              else "there was no PIN to disable")
        return 0

    if verb == "set":
        if auth_service.enabled(settings):
            print("a PIN is already set — use 'change' (or 'reset' if it is forgotten)",
                  file=sys.stderr)
            return 1
        pin = _offer(f"New PIN ({MIN_DIGITS}-{MAX_DIGITS} digits): ")
        if pin is None:
            return 1
        result = auth_service.create(settings, pin, label=settings.instrument or "")
        print("the lock is ON — the next request from a browser is sent to /login.")
        return _report(result)

    if verb == "change":
        current = str(_read_secret("Current PIN: ")).strip()
        pin = _offer(f"New PIN ({MIN_DIGITS}-{MAX_DIGITS} digits): ")
        if pin is None:
            return 1
        result = auth_service.change_pin(settings, current, pin)
        if result.get("ok"):
            print("every other device is now signed out.")
        return _report(result)

    # reset
    pin = _offer(f"New PIN ({MIN_DIGITS}-{MAX_DIGITS} digits): ")
    if pin is None:
        return 1
    result = auth_service.reset(settings, pin)
    if result.get("ok"):
        print("every session is now signed out.")
    return _report(result)


if __name__ == "__main__":  # pragma: no cover - the CLI's own entry point
    raise SystemExit(main())
