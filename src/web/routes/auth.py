"""The lock's own endpoints, and the screen itself.

Each answers a question the lock screen asks:

* ``GET /status`` — is there a lock, am I through it, and how long may I sit idle?
* ``POST /setup`` — the FIRST PIN. Refused the moment one exists; that refusal is what makes the
  create form safe to serve to anyone, and it is also the reason the CLI's ``set`` and this mean
  the same thing. Whoever does it is signed in by the answer, so the page can go straight on.
* ``POST /login`` — here is the PIN. A wrong one is 401 with the reason (and how many tries are
  left); a locked-out one is 429, because "wait" is a different instruction from "try again".
* ``POST /change`` — the CURRENT PIN, then a new one, from the lock screen's own link or the
  monitor's Security card. It feeds the same failed-attempt counter as ``/login``, and every other
  session is signed out by it; the caller gets a fresh cookie so the change does not throw them out
  of the page they are standing on.
* ``POST /sign-out-everywhere`` — the session you are not looking at. Needs a session of your own,
  keeps the PIN, and expires every cookie except the one it is answered with.
* ``POST /logout`` — forget the session.
* ``POST /heartbeat`` — a person is still here, so keep the session's idle clock moving. Nothing
  else may: the monitor polls every twenty seconds, and a poll is not a person.

``GET /login`` serves the screen. It is the same component the pages raise over themselves, so a
cold start and an expired session look and behave identically — and it is reachable on purpose, by
typing the address, because it is also where a PIN is created and changed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

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


class ChangeBody(BaseModel):
    current: str = ""
    pin: str = ""


def _signed_in(result: Dict[str, Any], request: Request, settings, **extra: Any) -> JSONResponse:
    """A session's answer: the payload, and the cookie that goes with it.

    Issued AFTER whatever rotated the secret, so the value handed back here is signed with the new
    one — otherwise the caller would be told they are signed in while holding a dead cookie.
    """
    body: Dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "signed_in": True,
        "user": result.get("user") or auth_service.DEFAULT_USER_ID,
        "idle_seconds": result.get("idle_seconds") or auth_service.DEFAULT_IDLE_SECONDS,
        **extra,
    }
    response = JSONResponse(body, status_code=200)
    token = auth_service.issue(settings, str(body["user"]))
    if token is not None:
        middleware.set_cookie(response, token, request)
    return response


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


@router.post("/setup")
def setup(body: PinBody, request: Request):
    """The FIRST PIN, written from the lock screen.

    Refused with 409 the moment one exists — so the page can offer this form to anybody who can
    reach it without that becoming a way to replace somebody's PIN. Whoever sets it is signed in by
    this answer, because they have just proved they are the person at the keyboard.
    """
    settings = get_settings()
    if auth_service.enabled(settings):
        return JSONResponse(
            {"ok": False, "enabled": True, "reason": "a PIN is already set — use Change PIN"},
            status_code=409,
        )
    try:
        result = auth_service.create(settings, body.pin, label=settings.instrument or "")
    except FileExistsError:  # somebody got there between the check and here
        return JSONResponse(
            {"ok": False, "enabled": True, "reason": "a PIN is already set — use Change PIN"},
            status_code=409,
        )
    if not result.get("ok"):
        return JSONResponse({"ok": False, "enabled": False, "reason": result.get("reason")},
                            status_code=400)
    logger.warning("The portal's first PIN was set from the lock screen")
    return _signed_in(result, request, settings, created=True)


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

    logger.info("A session was opened for %r", result.get("user"))
    return _signed_in(result, request, settings)


@router.post("/change")
def change(body: ChangeBody, request: Request):
    """Replace the PIN, given the current one. Signs out every OTHER session.

    Public by necessity — this is the page that fixes a PIN you no longer trust, and it cannot
    require the session it is about to invalidate. What protects it is the current PIN, counted by
    the same failures that lock ``/login``: five wrong ones and this door is shut as well.
    """
    settings = get_settings()
    problem = auth_service.pin_problem(body.pin)
    if problem:
        return JSONResponse({"ok": False, "reason": problem}, status_code=400)

    result = auth_service.change_pin(settings, body.current, body.pin)
    if not result.get("ok"):
        status_code = 429 if result.get("locked") else 401
        return JSONResponse(
            {"ok": False, "enabled": True, "signed_in": False,
             "locked": bool(result.get("locked")), "reason": result.get("reason") or "refused"},
            status_code=status_code,
        )
    logger.warning("The PIN was changed from the portal")
    # A fresh cookie in the same answer, whatever the old one was: the change rotated the secret, so
    # the caller would otherwise be told "done" while holding a session that no longer works. They
    # have just proved they know both PINs, so they are the one person who may stay signed in.
    return _signed_in(result, request, settings, changed=not result.get("unchanged"),
                      unchanged=bool(result.get("unchanged")))


@router.post("/sign-out-everywhere")
def sign_out_everywhere(request: Request):
    """Expire every session but this one. Needs a session of its own, keeps the PIN."""
    settings = get_settings()
    if auth_service.read(settings, request.cookies.get(middleware.COOKIE_NAME)) is None:
        return JSONResponse({"ok": False, "signed_in": False,
                             "reason": "signed out — enter the PIN"}, status_code=401)
    result = auth_service.sign_out_others(settings)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "reason": result.get("reason")}, status_code=409)
    logger.warning("Every other session was signed out from the portal")
    return _signed_in(result, request, settings, signed_out_others=True)


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

    The answer carries the window in force as well, because this is the only traffic a busy page
    sends of its own accord: it is how a tab that did NOT change the setting hears about it.
    """
    settings = get_settings()
    token = auth_service.touch(settings, request.cookies.get(middleware.COOKIE_NAME))
    if token is None:
        return JSONResponse({"ok": False, "signed_in": False}, status_code=401)
    response = JSONResponse(
        {
            "ok": True,
            "signed_in": True,
            "idle_seconds": auth_service.idle_seconds(settings),
        },
        status_code=200,
    )
    middleware.set_cookie(response, token, request)
    return response
