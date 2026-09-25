"""The gate: what needs a session, what does not, and what the lock must never touch.

The route walk is the point of this file. A hand-written list of "paths to protect" rots the first
time somebody adds an endpoint, so instead every path the app registers is exercised with no cookie
and has to be refused — and the short list that is ALLOWED to answer is pinned, so a new public
prefix cannot appear without a test changing.

And one property that outranks the rest: **the trading switch is not this module's business.** A
locked portal is a locked browser. The loop is a separate process reading files, so a lockout, a
refused request or a sign-out must leave ``data/trading.json`` byte-identical — that is what makes
locking the screen safe to do at all.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from src.config.settings import Settings
from src.config import trading_state
from src.web import middleware
from src.web.app import app
from src.web.routes import auth as auth_routes
from src.web.services import auth_service

PIN = "4821"
COOKIE = middleware.COOKIE_NAME

#: The only paths that may answer without a session. Pinned on purpose: a new prefix means a new
#: hole, and the way to add one should be to change this list and think about it.
EXPECTED_PUBLIC = {
    "/login",
    "/api/v1/health",
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/heartbeat",
}


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Settings:
    """A tmp store, and the gate and the routes both pointed at it."""
    built = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        historical_data_dir=str(tmp_path / "data" / "historical"),
        strategy_rules_file=str(tmp_path / "store.json"),
        instrument="NVDA",
    )
    monkeypatch.setattr(middleware, "get_settings", lambda: built)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: built)
    monkeypatch.setattr("src.config.trading_state.active_strategy_name", lambda: None)
    return built


@pytest.fixture()
def client(settings) -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def locked(settings) -> Settings:
    """A portal with a PIN set — the state everything below is about."""
    auth_service.create(settings, PIN, label="NVDA")
    return settings


def _sample(path: str) -> str:
    """A concrete request for a route with parameters: the gate decides before routing does."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _routes() -> list:
    """Every (method, path) the app serves, minus the mount and the docs pages."""
    found: dict = {}
    for route in app.routes:
        path = str(getattr(route, "path", "") or "")
        if not path or path.startswith(("/static", "/docs", "/redoc", "/openapi")):
            continue
        methods = set(getattr(route, "methods", None) or {"GET"})
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            found.setdefault(path, set()).add(method)
    return sorted((method, path) for path, methods in found.items() for method in methods)


# ---------------------------------------------------------------------------
# with no PIN set, nothing changes
# ---------------------------------------------------------------------------
def test_with_no_store_the_portal_is_exactly_as_it_was(client):
    """The lock is off until somebody sets one, which is what keeps a fresh checkout working."""
    for method, path in (("GET", "/"), ("GET", "/log"), ("GET", "/api/v1/loop"),
                         ("GET", "/api/v1/positions")):
        response = client.request(method, _sample(path))
        assert response.status_code == 200, f"{method} {path} answered {response.status_code}"


# ---------------------------------------------------------------------------
# the route walk: everything is refused, except the pinned allowlist
# ---------------------------------------------------------------------------
def test_every_route_needs_a_session_except_the_ones_that_say_they_do_not(client, locked):
    """No cookie, so every path is either explicitly public or refused."""
    for method, path in _routes():
        sample = _sample(path)
        if middleware.is_public(path):
            assert path in EXPECTED_PUBLIC, f"{path} is public but not on the pinned list"
            continue
        response = client.request(method, sample)
        assert response.status_code in (401, 303), (
            f"{method} {path} answered {response.status_code} with no session"
        )


def test_the_allowlist_is_exactly_the_pinned_one(locked):
    """The other half: no path may be public that is not supposed to be."""
    public = {path for _method, path in _routes() if middleware.is_public(path)}

    assert public == EXPECTED_PUBLIC


def test_a_page_is_sent_to_the_lock_screen_and_an_api_call_is_told_why(client, locked):
    page = client.get("/log")
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")
    assert "next=%2Flog" in page.headers["location"], "and back to where they were going"

    api = client.get("/api/v1/loop")
    assert api.status_code == 401
    assert api.json()["signed_in"] is False

    health = client.get("/api/v1/health")
    assert health.status_code == 200 and health.json() == {"status": "ok"}


def test_the_login_page_itself_is_reachable(client, locked):
    response = client.get("/login")
    assert response.status_code == 200
    assert "lock-root" in response.text and "/static/auth.js" in response.text


# ---------------------------------------------------------------------------
# getting in, and staying in
# ---------------------------------------------------------------------------
def test_a_wrong_pin_is_refused_with_a_reason_and_the_right_one_lets_you_in(client, locked):
    refused = client.post("/api/v1/auth/login", json={"pin": "0000"})
    assert refused.status_code == 401
    assert "attempt" in refused.json()["reason"], "the operator is told what is left"

    ok = client.post("/api/v1/auth/login", json={"pin": PIN})
    assert ok.status_code == 200 and ok.json()["signed_in"] is True
    assert COOKIE in ok.cookies or COOKIE in ok.headers.get("set-cookie", "")

    assert client.get("/log").status_code == 200, "and the page is no longer refused"
    assert client.get("/api/v1/loop").status_code == 200


def test_the_cookie_is_locked_down(client, locked):
    ok = client.post("/api/v1/auth/login", json={"pin": PIN})
    header = ok.headers["set-cookie"]

    assert "HttpOnly" in header, "no script may read it"
    assert "samesite=strict" in header.lower(), "and no other site may cause it to be sent"
    assert "Secure" not in header, "not over plain HTTP, or the browser would drop it"


def test_a_cookie_that_is_not_yours_gets_you_nothing(client, locked):
    client.cookies.set(COOKIE, "made.up")
    assert client.get("/api/v1/loop").status_code == 401

    issued = str(auth_service.issue(locked))
    client.cookies.set(COOKIE, issued[:-2] + "xx")
    assert client.get("/api/v1/loop").status_code == 401, "a signature that does not check out"


def test_a_lockout_is_429_and_not_another_401(client, locked):
    """'Wait' is a different instruction from 'try again'."""
    for _attempt in range(auth_service.MAX_ATTEMPTS):
        client.post("/api/v1/auth/login", json={"pin": "0000"})

    response = client.post("/api/v1/auth/login", json={"pin": PIN})

    assert response.status_code == 429
    assert response.json()["locked"] is True


def test_signing_out_forgets_the_session(client, locked):
    client.post("/api/v1/auth/login", json={"pin": PIN})
    assert client.get("/api/v1/loop").status_code == 200

    assert client.post("/api/v1/auth/logout").status_code == 200

    assert client.get("/api/v1/loop").status_code == 401


def test_the_status_endpoint_says_whether_a_session_is_alive(client, locked):
    anonymous = client.get("/api/v1/auth/status").json()
    assert anonymous["enabled"] is True and anonymous["signed_in"] is False
    assert anonymous["idle_seconds"] == auth_service.DEFAULT_IDLE_SECONDS

    client.post("/api/v1/auth/login", json={"pin": PIN})

    signed_in = client.get("/api/v1/auth/status").json()
    assert signed_in["signed_in"] is True and signed_in["user"] == "owner"


# ---------------------------------------------------------------------------
# the machine token, and the one thing none of this may touch
# ---------------------------------------------------------------------------
def test_the_loop_watchdog_gets_in_with_a_token_and_not_with_a_session(client, settings, locked):
    """A cron cannot hold a cookie, and the monitor's poll stops when nobody is signed in."""
    token = auth_service.load(settings)["machine_token"]

    without = client.post("/api/v1/loop/ensure")
    assert without.status_code == 401, "no cookie, no token, no"

    with_token = client.post("/api/v1/loop/ensure",
                             headers={middleware.TOKEN_HEADER: token})
    assert with_token.status_code != 401, "the cron is let through"


def test_the_token_opens_that_one_door_and_no_others(client, settings, locked):
    token = auth_service.load(settings)["machine_token"]

    response = client.get("/api/v1/trading", headers={middleware.TOKEN_HEADER: token})

    assert response.status_code == 401, "a cron has no business reading the switch"


def test_a_cross_origin_post_is_refused_even_with_a_session(client, locked):
    """A page on another local port is a different origin on the same site."""
    client.post("/api/v1/auth/login", json={"pin": PIN})

    response = client.post("/api/v1/trading/off-flatten",
                           headers={"Origin": "http://evil.localhost:3000"})

    assert response.status_code == 403
    assert "did not come from the portal" in response.json()["reason"]


def test_same_origin_posts_still_work(client, locked):
    client.post("/api/v1/auth/login", json={"pin": PIN})

    response = client.post("/api/v1/auth/heartbeat", headers={"Origin": "http://testserver"})

    assert response.status_code == 200, "the portal's own pages are allowed"


def test_the_lock_never_touches_the_trading_switch(client, settings, locked):
    """The invariant the whole design rests on: the loop reads that file, and never this module."""
    trading_state.write_state(settings, {"on": True, "strategy": "NVDA", "env": "paper"})
    switch = auth_service.store_path(settings).parent / "trading.json"
    before = switch.read_text(encoding="utf-8")

    client.get("/log")                                     # refused
    client.post("/api/v1/trading/on")                      # refused
    for _attempt in range(auth_service.MAX_ATTEMPTS):      # locked out
        client.post("/api/v1/auth/login", json={"pin": "0000"})
    client.post("/api/v1/auth/login", json={"pin": PIN})   # and let in
    client.get("/api/v1/loop")
    client.post("/api/v1/auth/logout")

    assert switch.read_text(encoding="utf-8") == before, "byte for byte"
    assert json.loads(before)["on"] is True, "and trading is still ON"
    assert trading_state.is_trading_on(settings) is True
