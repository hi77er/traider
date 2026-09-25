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
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.config.settings import get_settings
from src.web.services import auth_service

logger = logging.getLogger(__name__)

__all__ = [
    "COOKIE_NAME",
    "MACHINE_PATHS",
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
        # EVERY mutating request, including the lock's own public doors. A PIN is set and changed
        # from pages that cannot have a session yet, so this is the only cross-origin check they
        # get, and asking for a JSON body is not one.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            logger.warning("Refused a cross-origin %s to %s", request.method, request.url.path)
            return JSONResponse(
                {"ok": False, "reason": "this request did not come from the portal"}, status_code=403
            )

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

        # The session's idle clock is slid by the HEARTBEAT alone — the one request a page sends
        # only when it has seen a human. Sliding on every authenticated request was the permissive
        # half of this design and it undid the point of it: the monitor polls every twenty seconds,
        # so a page nobody is touching kept its own session alive for ever. It also broke SHORT
        # windows outright, because the slide waited five minutes before it would move a clock
        # that a one- or three-minute window had already run out — see
        # ``routes/auth.py::heartbeat``, which is now the only thing that may.
        return await call_next(request)
