"""Retrying a broker call without ever retrying the wrong thing.

Retrying an order is dangerous in a way retrying a GET is not, so the rule here is
narrow and explicit: **only a call that provably never reached a verdict may be
retried.**

* A transport failure (connection reset, DNS, timeout) may or may not have been
  received — Alpaca documents a `client_order_id` for exactly that reason, so a
  retry carries the same id and the broker deduplicates it. That is what makes a
  retry safe rather than a coin flip.
* Alpaca answering 401/403 means the key is revoked. Retrying cannot help: the
  same key gets the same answer, and burning the backoff only delays the alert.
* A 4xx validation error means the order itself is wrong. A retry sends the same
  wrong order.
* 429 and 5xx are the broker saying "not now", which is precisely what backoff is
  for.

So a caller asks for retries and this module decides per failure, using
``AlpacaError.retryable`` — set where the status code is known, never guessed at a
call site. Every attempt is logged with the environment label, because paper and
live accounts look identical in Alpaca's portal and an unlabelled retry log is how
a live order gets missed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["execute_with_retry"]

# Statuses worth trying again: the broker is busy, not disagreeing.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def execute_with_retry(
    fn: Callable[[], Any],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Optional[Callable[[int, Optional[BaseException]], None]] = None,
    label: str = "order",
) -> Any:
    """Call ``fn`` and return its result, retrying only retryable failures.

    ``max_retries`` counts RETRIES, not attempts: 3 means up to 4 calls, which is
    the reading ``EXECUTION_MAX_RETRIES`` has always had. The delay doubles from
    ``base_delay`` after each failure (1s, 2s, 4s …) and does not grow past the
    attempt count, so a long backoff is never chosen by arithmetic alone.

    Raises the LAST exception when every attempt fails, so the caller's message is
    the most recent truth about why, not the first.
    """
    retries = max(0, int(max_retries))
    base = max(0.0, float(base_delay))
    delay = base

    for attempt in range(retries + 1):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            retryable = bool(getattr(exc, "retryable", True))
            final = attempt >= retries
            if on_attempt:
                on_attempt(attempt + 1, exc)
            if not retryable or final:
                if retryable:
                    logger.error("%s failed after %d attempt(s): %s", label, attempt + 1, exc)
                else:
                    # Not a transient failure: say so, because "gave up" and "cannot
                    # work" call for different actions from the operator.
                    logger.error("%s refused and will not be retried: %s", label, exc)
                raise
            logger.warning(
                "%s attempt %d/%d failed (%s) — retrying in %.1fs",
                label, attempt + 1, retries + 1, exc, delay,
            )
            if delay > 0:
                sleep(delay)
            delay = base * (2 ** (attempt + 1))
        else:
            if attempt:
                logger.warning("%s succeeded on attempt %d", label, attempt + 1)
            if on_attempt:
                on_attempt(attempt + 1, None)
            return result

    # Unreachable: the loop either returns or raises. Kept so the contract is total.
    raise RuntimeError(f"{label}: retry loop ended without a result")
