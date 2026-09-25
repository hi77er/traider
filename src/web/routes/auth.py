"""The lock's own endpoints, and the screen itself.

Four of them, and each answers a question the lock screen asks:

* ``GET /status`` — is there a lock, am I through it, and how long may I sit idle?
* ``POST /login`` — here is the PIN. A wrong one is 401 with the reason (and how many tries are
  left); a locked-out one is 429, because "wait" is a different instruction from "try again".
* ``POST /logout`` — forget the session.
* ``POST /heartbeat`` — a person is still here, so keep the session's idle clock moving. Nothing
  else may: the monitor polls every twenty seconds, and a poll is not a person.

``GET /login`` serves the screen. It is the same component the pages raise over themselves, so a
cold start and an expired session look and behave identically.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.config.settings import get_settings
from src.web import middleware
from src.web.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
page_router = APIRouter()

_LOGIN = Path(__file__).resolve().parents[1] / "templates" / "login.html"


class PinBody(BaseModel):
    pin: str = ""


@page_router.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    """The lock screen, served as a page for a cold start."""
    return FileResponse(_LOGIN)


@router.get("/status")
def status(request: Request) -> dict:
    """What the page needs to decide whether to raise the overlay."""
    settings = get_settings()
    described = auth_service.describe(settings)
    payload = auth_service.read(settings, request.cookies.get(middleware.COOKIE_NAME))
    return {
        "ok": True,
        "enabled": described["enabled"],
        "signed_in": payload is not None,
        "user": described["user"],
        "label": described["label"],
        "idle_seconds": described["idle_seconds"],
        "updated_at": described["updated_at"],
        "locked_until": described["locked_until"],
    }


@router.post("/login")
def login(body: PinBody, request: Request):
    """Check a PIN and hand out a session."""
    settings = get_settings()
    if not auth_service.enabled(settings):
        return JSONResponse({"ok": True, "enabled": False, "signed_in": True}, status_code=200)

    result = auth_service.verify(settings, body.pin)
    if not result.get("ok"):
        status_code = 429 if result.get("locked") else 401
        return JSONResponse(
            {"ok": False, "enabled": True, "signed_in": False,
             "locked": result.get("locked", False), "reason": result.get("reason") or "refused"},
            status_code=status_code,
        )

    token = auth_service.issue(settings, str(result.get("user") or auth_service.DEFAULT_USER_ID))
    if token is None:  # pragma: no cover - the store changed under us
        return JSONResponse({"ok": False, "reason": "the PIN was accepted but no user exists"},
                            status_code=500)
    response = JSONResponse(
        {"ok": True, "enabled": True, "signed_in": True, "user": result.get("user"),
         "idle_seconds": result.get("idle_seconds")},
        status_code=200,
    )
    middleware.set_cookie(response, token, request)
    logger.info("A session was opened for %r", result.get("user"))
    return response


@router.post("/logout")
def logout() -> JSONResponse:
    """Forget the session. The loop is not told, and could not care."""
    response = JSONResponse({"ok": True, "enabled": auth_service.enabled(get_settings()),
                             "signed_in": False}, status_code=200)
    middleware.clear_cookie(response)
    return response


@router.post("/heartbeat")
def heartbeat(request: Request) -> JSONResponse:
    """One request per few minutes, and only while the page can see a person at the keyboard.

    This is the ONLY thing that moves a session's idle clock, which is what stops the dashboard's
    own polling from holding the door open for a screen nobody is sitting at.
    """
    settings = get_settings()
    token = auth_service.touch(settings, request.cookies.get(middleware.COOKIE_NAME))
    if token is None:
        return JSONResponse({"ok": False, "signed_in": False}, status_code=401)
    response = JSONResponse({"ok": True, "signed_in": True}, status_code=200)
    middleware.set_cookie(response, token, request)
    return response
