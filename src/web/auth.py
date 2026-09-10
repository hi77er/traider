"""HTTP Basic auth for the Web Portal.

Gated by config:
- ``WEB_PORTAL_ENABLED``  — False disables the whole portal.
- ``WEB_PORTAL_AUTH_ENABLED`` — False disables login.
- ``WEB_PORTAL_PASSWORD`` — if empty (dev default) no credentials are required.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.config.settings import Settings, get_settings

_basic = HTTPBasic(auto_error=False)


def require_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic),
    settings: Settings = Depends(get_settings),
) -> Optional[HTTPBasicCredentials]:
    """Return credentials when auth is satisfied, else raise 401."""
    if not settings.web_portal_enabled:
        return None
    if not settings.web_portal_auth_enabled or not settings.web_portal_password:
        return None  # auth disabled, or no password configured (local dev)

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.web_portal_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.web_portal_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials
