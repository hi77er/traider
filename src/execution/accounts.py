"""What each account is WORTH — equity, the day's change, and the cash behind it.

``positions`` answers "what do I hold"; this answers "what is it worth". The dashboard needs
both, and they are different questions with different failure modes: a broker that refuses to
list positions has told you nothing about equity either, but an account with no credentials is
provably flat while its equity is simply unknown. So the degrade paths here are NOT the same
as the ones next door, and the difference is deliberate.

**Both environments, always.** The same reason ``positions`` reads both: paper and live are
different accounts, and the one this screen is not pointed at is exactly the one an operator
forgets to check. A strategy can only trade one at a time, but a person can be wrong about
which.

**What this must never do is proxy the broker's payload.** ``GET /v2/account`` carries
``account_number`` and ``id``, and a dashboard has no business forwarding those to a browser.
Every field is therefore named explicitly and the account number is masked to its last four —
which is stricter than the credentials badge already on screen, and enough to answer the one
question it is for: *which* account are these numbers for.

**Equity is a number the bot already reads.** ``AlpacaExecutor.equity`` is called on every
entry to turn a weight into a share count, so deciding to trade and displaying what you are
trading with fetch the same field. Nothing here is new information — it is just no longer
thrown away.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.execution import credentials
from src.execution.alpaca_client import AlpacaError
from src.execution.alpaca_executor import AlpacaExecutor
from src.execution.config import ExecutionConfigError, execution_status
from src.execution.positions import ENVS, executor_for

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_SECONDS",
    "EnvAccount",
    "forget",
    "snapshot",
    "snapshots",
]

#: Half a minute. Longer than the positions cache on purpose: a position changes when an order
#: fills, which is the event this panel exists to show, while equity drifts with the market and
#: is never urgent to the second. It is also what a weight gets sized against, so a number that
#: lags a little still answers "what am I trading with".
CACHE_SECONDS = 30.0

#: The fields worth showing, named one by one — see the module docstring for why this is a
#: projection rather than the payload.
_FIELDS = (
    "equity",
    "last_equity",
    "cash",
    "buying_power",
    "portfolio_value",
    "long_market_value",
    "short_market_value",
    "currency",
    "status",
    "multiplier",
    "trading_blocked",
    "account_blocked",
)

#: Alpaca sends money as STRINGS (``"equity": "100000"``) — JSON-decimal style, so a client
#: cannot round it in binary float. Passing them through unchanged would leave this payload
#: with two types per concept: ``equity`` a string and ``day_pl`` — derived here — a number.
#: Anything comparing or adding the two would have to know which is which, so the conversion
#: happens once, at the boundary, and the browser gets one type per field.
_NUMERIC = frozenset(
    {
        "equity",
        "last_equity",
        "cash",
        "buying_power",
        "portfolio_value",
        "long_market_value",
        "short_market_value",
        "multiplier",
    }
)

_now = time.monotonic

_cache: Dict[str, Tuple[float, "EnvAccount"]] = {}


@dataclass(frozen=True)
class EnvAccount:
    """One account's worth, or why it could not be read.

    ``known`` False means "we could not find out", which is not the same as zero and must
    never render as ``$0.00`` — a zero balance and an unreadable account look identical on a
    panel and mean opposite things to the person reading it.
    """

    env: str
    fields: Tuple[Tuple[str, Any], ...] = ()
    known: bool = True
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """The shape the dashboard renders: the projected fields plus what is derived."""
        out: Dict[str, Any] = {
            "env": self.env,
            "known": self.known,
            "reason": self.reason,
            "account": "",
            "equity": None,
            "last_equity": None,
            "day_pl": None,
            "day_pl_pct": None,
            "cash": None,
            "buying_power": None,
            "portfolio_value": None,
            "currency": "",
            "status": "",
            "blocked": False,
        }
        if not self.known:
            return out

        values = dict(self.fields)
        for key in _FIELDS:
            if values.get(key) is None:
                continue
            if key in ("trading_blocked", "account_blocked"):
                continue
            out[key] = _number(values[key]) if key in _NUMERIC else values[key]

        out["account"] = _masked(values.get("account_number"))
        out["blocked"] = bool(values.get("trading_blocked") or values.get("account_blocked"))

        equity = _number(values.get("equity"))
        last = _number(values.get("last_equity"))
        out["day_pl"] = None if equity is None or last is None else round(equity - last, 2)
        # A percentage of nothing is not zero percent — with no previous close there is no
        # denominator, and printing "0.00%" would report a flat day that was never measured.
        if out["day_pl"] is not None and last:
            out["day_pl_pct"] = round((equity - last) / last * 100.0, 4)
        return out


def _number(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _masked(account_number: Any) -> str:
    """``PA36Z5MM1R9V`` -> ``**** **** R9V``. Enough to tell two accounts apart, and no more."""
    text = str(account_number or "").strip()
    return f"****{text[-3:]}" if len(text) > 3 else ""


def probe(viewed, env: str) -> Dict[str, Any]:
    """The broker call itself — the single door to ``GET /v2/account``.

    Module-level and separate from the cache for the same reason ``positions.probe`` is: a test
    patches THIS one name and the whole read becomes offline, with no session to stub. The
    alternative is worse than it sounds — a real request in a test returns 401, and every
    number here would silently become "unknown", so a test asserting the happy path would pass
    for the wrong reason.
    """
    return executor_for(viewed, env).account()


def _viewed(settings, env: str):
    """``settings`` pointed at ``env``, without touching the caller's object."""
    return settings.model_copy(update={"execution_env": env})


def _cache_key(status: Dict[str, Any], env: str, settings) -> str:
    """Identifies the ACCOUNT: base URL, environment, and a fingerprint of the key.

    Same key shape as the positions cache, and for the same reason: a rotated key is a
    different account, and an answer about the old one must not survive it.
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


def _read(viewed, env: str, status: Dict[str, Any]) -> EnvAccount:
    if not status.get("ok") and not _keys_configured(status, env):
        # No key at all: there is no account behind it and nothing to ask. Reported as
        # unknown rather than as zero, because "$0.00" is a claim about a balance and this
        # is the absence of one.
        return EnvAccount(
            env=env,
            known=False,
            reason=f"no {env} credentials are configured, so this account cannot be read",
        )

    try:
        payload = probe(viewed, env)
    except (AlpacaError, ExecutionConfigError) as exc:
        logger.warning("Could not read the %s account: %s", env, exc)
        return EnvAccount(env=env, known=False, reason=f"the {env} account could not be read ({exc})")
    except Exception as exc:  # noqa: BLE001 - a panel must not 500 over a number
        logger.exception("Unexpected failure reading the %s account", env)
        return EnvAccount(env=env, known=False, reason=f"the {env} account could not be read ({exc})")

    if not isinstance(payload, dict) or not payload:
        return EnvAccount(env=env, known=False, reason=f"the {env} account came back empty")

    # The projection happens here rather than at the route, so the raw payload — account
    # number and id included — never leaves this function, whatever a future caller does.
    kept = {key: payload.get(key) for key in _FIELDS if key in payload}
    kept["account_number"] = payload.get("account_number")
    return EnvAccount(env=env, fields=tuple(kept.items()))


def snapshot(settings, env: str, *, force: bool = False) -> EnvAccount:
    """What ``env`` is worth, cached for half a minute. ``force`` re-reads it."""
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


def snapshots(settings, envs: Optional[Sequence[str]] = None, *, force: bool = False) -> List[EnvAccount]:
    """Both accounts by default — see the module docstring for why."""
    return [snapshot(settings, env, force=force) for env in (envs or ENVS)]


def forget() -> None:
    """Drop every cached answer. The test seam, and what a key change should call."""
    _cache.clear()
