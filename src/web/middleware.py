"""The portal's gate: one middleware, fail-closed, and blind to the loop.

**An allowlist, not a list of protected paths.** The failure mode of the other shape is a new
endpoint that nobody remembers to protect, so anything not named in :func:`is_public` needs a
session — and ``tests/test_web/test_auth_gate.py`` walks the app's own route table to prove it.

Two things this deliberately never does:

* **it does not touch ``data/trading.json``.** A locked portal is a locked BROWSER. The trading
  loop is a separate, detached process that reads files and does not know this module exists, so a
  lockout, a sign-out or a PIN change cannot stop, start or alter a run.
* **it does not resolve per-strategy settings.** The lock belongs to the machine, not to whichever
  strategy happens to be active.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.config import loop_state
from src.config.settings import get_settings
from src.web.services import auth_service

logger = logging.getLogger(__name__)

__all__ = [
    "COOKIE_NAME",
    "MACHINE_PATHS",
    "SLIDE_AFTER_SECONDS",
    "TOKEN_HEADER",
    "install",
    "is_public",
    "set_cookie",
]

#: The session cookie. ``HttpOnly`` so no script can read it, ``Strict`` so no other site can cause
#: it to be sent — which, with the Origin check below, is the whole of this app's CSRF story.
COOKIE_NAME = "traider_session"

#: How the loop's watchdog gets in without a session: a cron cannot hold a cookie. Only the one
#: endpoint that restores a crashed loop accepts it.
TOKEN_HEADER = "X-Traider-Token"
MACHINE_PATHS = ("/api/v1/loop/ensure",)

#: Re-issue the cookie at most this often. The monitor polls every twenty seconds, and a Set-Cookie
#: on every poll is traffic nobody asked for; five minutes is far inside any sane idle window.
SLIDE_AFTER_SECONDS = 300

#: Paths that need no session. Everything else does.
_PUBLIC_PREFIXES = ("/static/", "/api/v1/auth/")
_PUBLIC_PATHS = ("/login", "/api/v1/health", "/health", "/favicon.ico")


def is_public(path: str) -> bool:
    """Does this path work without a session? — the entire policy, in one place."""
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def set_cookie(response, value: str, request: Request) -> None:
    """Hand out a session. ``Secure`` only when the request really came over HTTPS.

    Which it usually has not: the portal is reached at ``localhost:8000``, or through a tunnel. A
    ``Secure`` cookie set over plain HTTP is simply dropped by the browser, so claiming it here
    would lock the operator out of their own dashboard on the machine it runs on.
    """
    forwarded = str(request.headers.get("x-forwarded-proto") or "").lower()
    secure = request.url.scheme == "https" or forwarded == "https"
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=auth_service.DEFAULT_ABSOLUTE_SECONDS,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _same_origin(request: Request) -> bool:
    """Is a MUTATING request from this portal's own pages?

    A cross-site ``POST`` that carries the cookie is the one attack the cookie's own protection
    cannot see: ``SameSite=Strict`` stops a browser sending it from another site, and this stops
    the rest — a page on another local port is a different origin on the same site. A request with
    no ``Origin`` at all is not a browser (curl, a script), and it gains nothing by lying here:
    without the cookie it has no session to use.
    """
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return True
    host = str(request.headers.get("host") or "")
    return origin.split("://")[-1].rstrip("/") == host


def _refuse(request: Request):
    """Send a browser to the lock screen, and tell an API client what happened."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "reason": "signed out — enter the PIN", "signed_in": False},
            status_code=401,
        )
    target = "/login"
    wanted = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if wanted and wanted != "/":
        target += f"?next={quote(wanted, safe='')}"
    return RedirectResponse(target, status_code=303)


def install(app: FastAPI) -> None:
    """Register the gate. Called last, so it is the OUTERMOST middleware."""

    @app.middleware("http")
    async def require_pin(request: Request, call_next):
        settings = get_settings()
        if not auth_service.enabled(settings) or is_public(request.url.path):
            return await call_next(request)

        if request.url.path in MACHINE_PATHS and auth_service.machine_ok(
            settings, request.headers.get(TOKEN_HEADER)
        ):
            return await call_next(request)

        token: Optional[str] = request.cookies.get(COOKIE_NAME)
        payload = auth_service.read(settings, token)
        if payload is None:
            return _refuse(request)

        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            logger.warning("Refused a cross-origin %s to %s", request.method, request.url.path)
            return JSONResponse(
                {"ok": False, "reason": "this request did not come from the portal"}, status_code=403
            )

        response = await call_next(request)
        seen = loop_state.parse_stamp(payload.get("seen"))
        stale = seen is None or (
            datetime.now(timezone.utc) - seen
        ).total_seconds() > SLIDE_AFTER_SECONDS
        if stale:
            slid = auth_service.touch(settings, token)
            if slid:
                set_cookie(response, slid, request)
        return response
