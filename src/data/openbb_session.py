"""Monkeypatch OpenBB's yfinance session to use ``curl_cffi`` (browser TLS
impersonation).

Yahoo aggressively rate-limits plain ``requests`` sessions because they lack a
browser TLS/JA3 fingerprint. yfinance's recommended backend is ``curl_cffi``
with ``impersonate="chrome"``, but ``openbb-yfinance`` builds a plain
``requests.Session`` via ``openbb_core.provider.utils.helpers.get_requests_session``
and passes it into ``yf.download(session=...)``.

This shim replaces that factory so yfinance receives a ``curl_cffi`` session —
dramatically reducing the chance of Yahoo rate-limiting the bot. See
https://github.com/ranaroussi/yfinance (``_http.py``) for the rationale.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# What the provider's session waits for when nothing else says: (connect, read) seconds, which
# curl_cffi turns into a connect timeout plus a total one. It treats ``timeout=None`` as "wait
# indefinitely", and neither OpenBB nor yfinance passes a value down, so this is the only bound
# on a read that stalls.
PROVIDER_TIMEOUT_SECONDS = (10.0, 60.0)

_PATCHED = False


def apply_openbb_session_patch() -> bool:
    """Patch OpenBB's ``get_requests_session`` to return a curl_cffi session.

    Idempotent. Returns True when the patch is active; False when curl_cffi is
    unavailable (OpenBB's default requests session is then used as-is).
    """
    global _PATCHED
    if _PATCHED:
        return True
    try:
        from curl_cffi import requests as curl_requests
        from openbb_core.provider.utils import helpers as core_helpers
    except ImportError as exc:  # pragma: no cover - depends on environment
        logger.warning("curl_cffi unavailable; yfinance may be rate-limited: %s", exc)
        return False

    original = core_helpers.get_requests_session

    def _patched(**kwargs):
        # Respect an explicitly supplied session (e.g. tests / custom setup).
        if "session" in kwargs:
            return original(**kwargs)
        # Build OpenBB's session to inherit its headers/proxy/verify settings.
        base = original(**kwargs)
        session = curl_requests.Session(
            impersonate="chrome", timeout=PROVIDER_TIMEOUT_SECONDS
        )
        session.headers.update(base.headers)
        if getattr(base, "verify", None) is not None:
            session.verify = base.verify
        if getattr(base, "proxies", None):
            session.proxies = base.proxies
        if getattr(base, "cert", None) is not None:
            session.cert = base.cert
        return session

    core_helpers.get_requests_session = _patched
    _PATCHED = True
    logger.info("Patched OpenBB get_requests_session -> curl_cffi (impersonate=chrome)")
    return True
