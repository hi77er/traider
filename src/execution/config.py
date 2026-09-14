"""Where an order goes: which Alpaca environment.

Alpaca's paper and live environments are the **same API** with a different base
URL and a different key pair, so "switching between them" is one triple swap.
Every other line of the executor is identical, which is why the decision lives
here alone: one resolver means one thing to test, and no second place that could
disagree about whether real money is at stake.

Two entry points, deliberately different in character:

* :func:`resolve_execution_target` **raises** when the configuration cannot be
  traded with. It is called immediately before an order, so a misconfigured bot
  refuses to trade rather than trading somewhere unexpected.
* :func:`execution_status` **never raises**. The dashboard badge and the backtest
  provenance block use it, because a missing credential must not stop the portal
  from rendering or a backtest from running.

Failure is always "refuse", never "fall back": silently downgrading a live
configuration to paper would hide a broken live setup, and silently upgrading a
paper configuration to live would spend real money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

# The only broker. Kept as a constant rather than hard-coded at each call site
# because it is also part of every run's provenance payload and of the report UI,
# so adding a second broker later extends this file instead of reshaping callers.
BROKER = "alpaca"
ENVIRONMENTS: Tuple[str, ...] = ("paper", "live")


class ExecutionConfigError(RuntimeError):
    """The effective settings cannot place an order. Refuse, never guess."""


@dataclass(frozen=True)
class ExecutionTarget:
    """Everything the executor needs to reach one broker environment."""

    broker: str
    env: str
    base_url: str
    key_id: str
    # repr=False so a stray log line or exception message can never print it.
    secret: str = field(repr=False)
    live: bool = False

    @property
    def label(self) -> str:
        return "LIVE" if self.live else "PAPER"


def _clean(value) -> str:
    """Normalize a settings value to a trimmed string (None -> "")."""
    return str(value).strip() if value is not None else ""


def _alpaca_target(settings, env: str) -> Tuple[str, str, str]:
    """``(key_id, secret, base_url)`` for an Alpaca environment."""
    if env == "live":
        return (
            _clean(settings.alpaca_live_api_key),
            _clean(settings.alpaca_live_api_secret),
            LIVE_BASE_URL,
        )
    return (
        _clean(settings.alpaca_paper_api_key),
        _clean(settings.alpaca_paper_api_secret),
        PAPER_BASE_URL,
    )


def resolve_execution_target(settings) -> ExecutionTarget:
    """Resolve the target for the next order, or raise :class:`ExecutionConfigError`."""
    env = _clean(settings.execution_env).lower() or "paper"

    if env not in ENVIRONMENTS:
        raise ExecutionConfigError(
            f"Unknown EXECUTION_ENV {env!r}; expected one of {list(ENVIRONMENTS)}."
        )

    key_id, secret, base_url = _alpaca_target(settings, env)

    if env == "live":
        # Two independent keys must agree, so that no single field flip (or a
        # mis-click in the settings form) can start trading real money.
        if not bool(getattr(settings, "execution_live_ack", False)):
            raise ExecutionConfigError(
                "EXECUTION_ENV=live but EXECUTION_LIVE_ACK is off — refusing to send "
                "REAL orders. Turn the acknowledgement on to confirm live trading."
            )
        if not key_id or not secret:
            raise ExecutionConfigError(
                "EXECUTION_ENV=live but the Alpaca LIVE API key/secret are missing — "
                "refusing to trade. (A paper key is not a substitute: paper and live "
                "keys are different.)"
            )
    elif not key_id or not secret:
        raise ExecutionConfigError(
            "The Alpaca PAPER API key/secret are not set — add them in Account "
            "Settings before placing simulated orders."
        )

    return ExecutionTarget(
        broker=BROKER,
        env=env,
        base_url=base_url,
        key_id=key_id,
        secret=secret,
        live=(env == "live"),
    )


def execution_status(settings) -> dict:
    """Non-raising summary of the execution configuration.

    ``ok`` False means "orders would be refused right now", which is exactly what
    the dashboard badge shows and what a run's provenance records — without ever
    including a credential.
    """
    broker = BROKER
    env = _clean(settings.execution_env).lower() or "paper"
    try:
        target = resolve_execution_target(settings)
    except ExecutionConfigError as exc:
        return {
            "broker": broker,
            "env": env,
            "live": env == "live",
            "base_url": LIVE_BASE_URL if env == "live" else PAPER_BASE_URL,
            "ok": False,
            "message": str(exc),
            "paper_keys_set": bool(_clean(settings.alpaca_paper_api_key)),
            "live_keys_set": bool(_clean(settings.alpaca_live_api_key)),
        }
    return {
        "broker": target.broker,
        "env": target.env,
        "live": target.live,
        "base_url": target.base_url,
        "ok": True,
        "message": (
            f"{target.label} · {target.broker} — real orders are enabled"
            if target.live
            else f"{target.label} · {target.broker} — simulated orders only"
        ),
        "paper_keys_set": bool(_clean(settings.alpaca_paper_api_key)),
        "live_keys_set": bool(_clean(settings.alpaca_live_api_key)),
    }
