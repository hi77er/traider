"""What the accounts hold, as a GATE asks — not as a tick asks.

Three actions can strand a position: arming the bot, switching the environment, and
changing or deleting the active strategy. All three ask the same question first, and
the answer must be good enough to REFUSE on, so this module is deliberately paranoid in
one direction: it only ever says "flat" when flat is either what the broker said or
something the configuration makes impossible.

Three ways to be flat:

1. **The broker said so** — the account was read and holds nothing.
2. **By construction** — no credentials are configured for that environment, so no order
   could ever have been placed there and there is nothing to orphan. This is NOT
   "treating an unreachable broker as flat": it is a proof from the configuration, and it
   is what keeps a data-only install (the README's default, no Alpaca keys) able to
   switch strategies at all.
3. Nothing else. A configured but unreachable account is **UNKNOWN**, and every caller
   refuses on unknown — with the reason, so the operator can act on it.

Both environments are read by default, not just the one in play. Paper and live are
different accounts, and switching paper->live strands a PAPER position just as thoroughly
as the reverse; an env-in-play-only check would refuse a switch for a reason it cannot
see. An environment with no credentials is flat by construction, so covering both costs
nothing until both are genuinely configured.

**Cached for a few seconds.** These checks sit on endpoints the dashboard calls in bursts
(render, save, re-render), and each one is a broker round trip. The cache is keyed by
account — base URL, environment and a fingerprint of the key — so swapping credentials
invalidates it without anyone having to remember to. The loop must NOT use this: it reads
the broker every tick, uncached, and that difference is deliberate.

Nothing here decides anything about trading. It reports what is held, and names what it
could not find out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.execution import credentials
from src.execution.alpaca_client import AlpacaError
from src.execution.alpaca_executor import AlpacaExecutor
from src.execution.config import ExecutionConfigError, execution_status

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_SECONDS",
    "ENVS",
    "EnvPositions",
    "describe",
    "executor_for",
    "forget",
    "held",
    "snapshot",
    "snapshots",
    "total",
    "unknown",
]

# How long a read stands in for a fresh one. Short enough that an operator who has just
# flattened a position sees that on the next click, long enough that rendering a page
# does not become three broker calls.
CACHE_SECONDS = 8.0

# The two accounts a strategy can route to. Deliberately not "whatever EXECUTION_ENV
# says": a position in the OTHER one is exactly what a gate has to catch.
ENVS = ("paper", "live")

# Injectable so a test can prove the TTL without sleeping, like the executors' ``sleep``.
_now = time.monotonic

_cache: Dict[str, Tuple[float, "EnvPositions"]] = {}


@dataclass(frozen=True)
class EnvPositions:
    """One account's positions, or why they could not be read.

    ``known`` False means "we could not find out", which is NOT the same as empty and must
    never be treated as such. ``by_construction`` means "the configuration makes a position
    impossible here" — flat because no key exists, not because a broker was asked.
    """

    env: str
    payloads: Tuple[Dict[str, Any], ...] = ()
    known: bool = True
    by_construction: bool = False
    reason: str = ""

    @property
    def flat(self) -> bool:
        """Nothing is held. False for an unknown account, on purpose."""
        return self.known and not self.payloads

    @property
    def count(self) -> int:
        return len(self.payloads)

    def as_dict(self) -> Dict[str, Any]:
        """The shape the dashboard and the turn-off report both render."""
        return {
            "env": self.env,
            "count": self.count,
            "known": self.known,
            "flat": self.flat,
            "by_construction": self.by_construction,
            "reason": self.reason,
            "positions": [_brief(p) for p in self.payloads],
        }


def _brief(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The few fields worth showing, so a payload is not pasted into a log verbatim."""
    if not isinstance(payload, dict):
        return {"symbol": str(payload)}
    return {
        "symbol": str(payload.get("symbol") or "").upper(),
        "qty": payload.get("qty"),
        "side": str(payload.get("side") or ""),
        "avg_entry_price": payload.get("avg_entry_price"),
        "market_value": payload.get("market_value"),
        "unrealized_pl": payload.get("unrealized_pl"),
    }


# -- the read --------------------------------------------------------------
def _viewed(settings, env: str):
    """``settings`` pointed at ``env``, without touching the caller's object.

    The executor resolves its environment from the settings it is handed, so asking about
    live means handing it live. Copying rather than mutating matters: the caller's settings
    object is the active strategy's, and the request may not be about it.
    """
    return settings.model_copy(update={"execution_env": env})


def executor_for(settings, env: str) -> AlpacaExecutor:
    """An executor pointed at ``env``.

    The one way to build an executor for an environment that is NOT the one in play, so no
    caller has to know that pointing settings at an account is how you get there.
    """
    return AlpacaExecutor(_viewed(settings, env))


def probe(viewed, env: str) -> List[Dict[str, Any]]:
    """The broker call itself — the single door to ``GET /v2/positions``.

    Module-level and separate from the cache on purpose, for the same reason
    ``credentials.probe`` is: a test patches THIS one name and the whole gate becomes
    offline, with no session to stub. That matters more than it looks — a real request in a
    test returns a 401, and a 401 is indistinguishable from an empty account to everything
    downstream, so the accident would quietly turn "a position is open" into "flat" and
    the test would pass for the wrong reason.
    """
    return executor_for(viewed, env).positions()


def _cache_key(status: Dict[str, Any], env: str, settings) -> str:
    """Identifies the ACCOUNT: base URL, environment, and a fingerprint of the key.

    Not the key itself — the fingerprint is already the project's non-reversible id for a
    pair (``credentials.fingerprint``), so a rotated key reads as a different account and
    the stale answer cannot survive it.
    """
    key_id = ""
    try:
        key_id = str(credentials.keys_for(settings, env).get("key_id") or "")
    except Exception:  # noqa: BLE001 - a cache key must never be the thing that fails
        key_id = ""
    return f"{status.get('base_url')}|{env}|{credentials.fingerprint(env, key_id)}"


def _keys_configured(status: Dict[str, Any], env: str) -> bool:
    if env == "live":
        return bool(status.get("live_keys_set"))
    return bool(status.get("paper_keys_set"))


def _read(viewed, env: str, status: Dict[str, Any]) -> EnvPositions:
    if not status.get("ok"):
        if env in ENVS and not _keys_configured(status, env):
            # Provably nothing there: no key, so no order was ever accepted for it.
            return EnvPositions(
                env=env,
                by_construction=True,
                reason=f"no {env} credentials are configured, so nothing can be held there",
            )
        return EnvPositions(
            env=env,
            known=False,
            reason=str(status.get("message") or f"the {env} account cannot be read"),
        )

    try:
        payloads = probe(viewed, env)
    except (AlpacaError, ExecutionConfigError) as exc:
        # Fail CLOSED. The account exists and cannot be read, so it may well hold
        # something — and "we could not ask" is not an answer anyone should trade on.
        logger.warning("Could not read %s positions: %s", env, exc)
        return EnvPositions(env=env, known=False, reason=f"the {env} account could not be read ({exc})")
    except Exception as exc:  # noqa: BLE001 - a gate must not 500
        logger.exception("Unexpected failure reading %s positions", env)
        return EnvPositions(env=env, known=False, reason=f"the {env} account could not be read ({exc})")

    return EnvPositions(env=env, payloads=tuple(payloads or ()))


def snapshot(settings, env: str, *, force: bool = False) -> EnvPositions:
    """What ``env`` holds, cached for a few seconds. ``force`` re-reads it."""
    env = str(env or "").strip().lower() or "paper"
    viewed = _viewed(settings, env)
    status = execution_status(viewed)

    key = _cache_key(status, env, viewed)
    now = _now()
    hit = _cache.get(key)
    if hit and not force and (now - hit[0]) < CACHE_SECONDS:
        return hit[1]

    answer = _read(viewed, env, status)
    _cache[key] = (now, answer)
    return answer


def snapshots(settings, envs: Optional[Sequence[str]] = None, *, force: bool = False) -> List[EnvPositions]:
    """Both accounts by default — see the module docstring for why."""
    return [snapshot(settings, env, force=force) for env in (envs or ENVS)]


def forget() -> None:
    """Drop every cached answer. The test seam, and what a flatten calls next."""
    _cache.clear()


# -- reading the answer ----------------------------------------------------
def held(items: Sequence[EnvPositions]) -> List[EnvPositions]:
    """Those that hold something."""
    return [p for p in items if p.payloads]


def unknown(items: Sequence[EnvPositions]) -> List[EnvPositions]:
    """Those that could not be read."""
    return [p for p in items if not p.known]


def total(items: Sequence[EnvPositions]) -> int:
    """How many positions are open across the accounts that could be read."""
    return sum(p.count for p in items)


def describe(items: Sequence[EnvPositions]) -> str:
    """One phrase naming what is held, for a refusal message.

    Names the SYMBOL and the account rather than the strategy: the position is the
    account's, and the strategy that opened it may not be the one on screen — which is
    exactly the situation these gates exist for.
    """
    parts: List[str] = []
    for item in held(items):
        first = _brief(item.payloads[0])
        qty = first.get("qty")
        what = f"{qty} {first['symbol']}".strip() if qty else (first["symbol"] or "a position")
        extra = f" (and {item.count - 1} more)" if item.count > 1 else ""
        parts.append(f"the {item.env} account holds {what}{extra}")
    for item in unknown(items):
        parts.append(f"the {item.env} account could not be read")
    return "; ".join(parts) or "no positions are open"
