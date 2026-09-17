"""Whether the exchange is open, and when it next changes — Alpaca's clock.

The dashboard needs one fact the loop's records do not carry. A tick says ``closed``, which
tells an operator the market is shut and nothing more; it does not say that it opens again at
09:30, which is the answer to the question they actually asked ("do I need to come back, and
when?"). A *tick* also cannot answer at all before the loop has ever run, which is exactly the
state a first test starts in.

So this reads the clock directly rather than scraping it out of the last tick. What that costs
is a broker call, which is why it is not folded into ``/api/v1/loop`` (file-only, polled every
few seconds) and why the answer is cached: the session boundary is the only thing that changes
here, so a minute-old answer is not stale in any way that matters.

The clock is an EXCHANGE fact, not an account one, but reading it still needs credentials, so
it is read through the environment in play. When those cannot be read the answer is UNKNOWN —
never "closed", which would be a confident lie about a different failure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.execution.alpaca_client import AlpacaError
from src.execution.config import ExecutionConfigError, execution_status
from src.execution.positions import executor_for

logger = logging.getLogger(__name__)

__all__ = ["CACHE_SECONDS", "forget", "state"]

#: Long enough that a page open all day costs one call a minute, short enough that a session
#: change is never more than a minute late on screen. The clock is not a live quote: it moves
#: at 09:30 and 16:00 and is otherwise constant.
CACHE_SECONDS = 60.0

#: Injectable so a test can prove the TTL without sleeping, like the executors' ``sleep``.
_now = time.monotonic

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def probe(executor) -> Dict[str, Any]:
    """The broker call itself — the single door to ``GET /v2/clock``.

    Module-level and separate from the cache for the same reason ``positions.probe`` is: a
    test patches THIS one name and the whole read becomes offline, with no session to stub.
    """
    return executor.clock()


def state(settings, *, force: bool = False) -> Dict[str, Any]:
    """The exchange's session, cached for a minute. ``force`` re-reads it."""
    status_info = execution_status(settings)
    env = str(status_info.get("env") or "paper").strip().lower() or "paper"

    if not status_info.get("ok"):
        # No usable credentials, so there is no clock to ask. Reported rather than raised:
        # this is the ordinary state of a data-only install, and the panel still has plenty
        # to say without it.
        return _unknown(str(status_info.get("message") or "the exchange clock needs credentials"))

    key = f"{status_info.get('base_url')}|{env}"
    now = _now()
    hit = _cache.get(key)
    if hit and not force and (now - hit[0]) < CACHE_SECONDS:
        return hit[1]

    answer = _read(settings, env)
    _cache[key] = (now, answer)
    return answer


def forget() -> None:
    """Drop the cached answer. The test seam, and what a key change should call."""
    _cache.clear()


def _read(settings, env: str) -> Dict[str, Any]:
    try:
        raw = probe(executor_for(settings, env))
    except (AlpacaError, ExecutionConfigError) as exc:
        logger.warning("Could not read the exchange clock: %s", exc)
        return _unknown(f"the exchange clock could not be read ({exc})")
    except Exception as exc:  # noqa: BLE001 - a panel must not 500 over a clock
        logger.exception("Unexpected failure reading the exchange clock")
        return _unknown(f"the exchange clock could not be read ({exc})")

    if not isinstance(raw, dict):
        return _unknown("the exchange clock came back in a shape the dashboard does not know")

    is_open = raw.get("is_open")
    if is_open is None:
        # A payload without ``is_open`` is not an answer about the session, and inferring
        # "closed" from a missing field is how a shut-looking market fabricates a wait.
        return _unknown("the exchange clock did not say whether the market is open")

    return {
        "ok": True,
        "env": env,
        "is_open": bool(is_open),
        "next_open": raw.get("next_open"),
        "next_close": raw.get("next_close"),
        "timestamp": raw.get("timestamp"),
        "message": "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _unknown(message: str) -> Dict[str, Any]:
    """No answer about the session. ``is_open`` is None, which is not "closed"."""
    return {
        "ok": False,
        "env": None,
        "is_open": None,
        "next_open": None,
        "next_close": None,
        "timestamp": None,
        "message": message,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
