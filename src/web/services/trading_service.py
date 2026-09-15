"""Trading on/off — the master switch, and the configuration lock it holds.

Trading starts **OFF** and is only ever turned on by an explicit action. While it
is on, nothing that would change what the bot is running may be modified: every
mutating settings endpoint (config, account, rules, dataset, delta) and the
backtest runner refuse. That is enforced **server-side**, not merely by disabling
buttons — a stale tab or a scripted POST must not be able to reconfigure a live
strategy mid-flight.

Turning trading ON while the environment is LIVE additionally requires a
per-request confirmation, so real money costs one deliberate extra action. That
confirmation is deliberately NOT a stored flag: the switch that starts trading is
the one the operator is looking at when they press it.

A key pair being *configured* is not the same as it *working*, so turning trading
on also requires the credential check for that environment to have passed (see
``src/execution/credentials.py``). Without it, "trading is ON" would be a promise
the bot cannot keep: the switch would sit there green while every order bounced off
a rejected key.

The state is runtime, not configuration — so it does not live in the strategy
store (which the lock itself would otherwise block), it lives beside the datasets
and is gitignored with them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, status

from src.config import state_files
from src.config.effective import active_strategy_name, get_effective_settings_dep
from src.execution import credentials
from src.execution.config import execution_status
from src.web.services import config_service

logger = logging.getLogger(__name__)

STATE_FILENAME = "trading.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(settings) -> Path:
    """``<data root>/trading.json`` — beside ``historical/`` and ``backtest_results/``."""
    return state_files.state_path(settings, STATE_FILENAME)


def _read(settings) -> dict:
    """Current state, defaulting to OFF. Never raises: an unreadable file must
    not be mistaken for "on"."""
    state = state_files.read_json(state_path(settings), {"on": False, "since": None})
    state["on"] = bool(state.get("on"))
    return state


def _write(settings, state: dict) -> None:
    """Atomic, owner-only write (mkstemp gives 0600)."""
    state_files.write_json(state_path(settings), state)


def is_trading_on(settings) -> bool:
    return bool(_read(settings).get("on"))


def get_state(settings) -> dict:
    return _read(settings)


def turn_on(settings, *, confirm_live: bool = False) -> dict:
    """Start trading, or explain why it cannot. Never raises."""
    status_info = execution_status(settings)

    # Fail closed: if an order could not actually be placed, "trading on" would be
    # a lie — the operator would believe the bot is live when it would refuse
    # every order.
    if not status_info["ok"]:
        return {
            "ok": False,
            "message": f"Trading cannot start — {status_info['message']}",
            "state": get_state(settings),
            "execution": status_info,
        }

    # Configured ≠ working. Which proof is required depends on what is at stake:
    #
    #   * PAPER — simulated orders, so there is nothing to lose by checking now
    #     rather than demanding that the operator pressed Validate first. The pair is
    #     verified at this moment (using a passing verdict if one is already on file,
    #     so this costs no call after the first time).
    #   * LIVE — real money, so the check must have happened BEFORE this click, and
    #     must belong to the key that is configured now. Being asked to look at a
    #     verdict is the point; discovering a bad key in the same click that arms the
    #     bot is not.
    env = status_info["env"]
    if status_info["live"]:
        unverified = credentials.require_verified(settings, env)
    else:
        check = credentials.verify(settings, env)
        unverified = None if check["ok"] else check["message"]
    if unverified:
        return {
            "ok": False,
            "needs_verification": True,
            "message": f"Trading cannot start — {unverified}",
            "state": get_state(settings),
            "execution": status_info,
            "credentials": credentials.check_for(settings, env),
        }

    if status_info["live"] and not confirm_live:
        return {
            "ok": False,
            "needs_live_confirmation": True,
            "message": (
                "This strategy routes orders to the LIVE Alpaca account, so real "
                "money is at stake. Confirm to start trading."
            ),
            "state": get_state(settings),
            "execution": status_info,
        }

    state = {
        "on": True,
        "since": _now_iso(),
        "env": status_info["env"],
        "broker": status_info["broker"],
        "base_url": status_info["base_url"],
    }
    _write(settings, state)
    logger.warning("TRADING TURNED ON (%s, %s)", state["env"].upper(), state["broker"])
    return {
        "ok": True,
        "message": f"Trading is ON — orders route to {state['env'].upper()}",
        "state": state,
        "execution": status_info,
    }


def turn_off(settings) -> dict:
    """Stop trading and release the configuration lock."""
    previous = get_state(settings)
    state = {
        "on": False,
        "since": None,
        "stopped_at": _now_iso(),
        "env": previous.get("env"),
        "broker": previous.get("broker"),
        "base_url": previous.get("base_url"),
    }
    _write(settings, state)
    logger.warning("TRADING TURNED OFF — configuration is unlocked again")
    return {
        "ok": True,
        "message": "Trading is OFF — settings can be changed again",
        "state": state,
        "execution": execution_status(settings),
    }


def payload(settings) -> dict:
    """Everything the header controls and the armed panel need."""
    state = get_state(settings)
    status_info = execution_status(settings)
    return {
        "trading": state,
        "locked": bool(state.get("on")),
        "execution": status_info,
        "env_options": config_service.EXECUTION_ENV_OPTIONS,
        "strategy": active_strategy_name(),
        "instrument": settings.instrument,
        "bar_size": settings.historical_bar_size,
        # Why the switch would refuse, so the label can say so before it is pressed:
        # the credentials for the environment in play must have been verified.
        "verification": credentials.check_for(settings, status_info["env"]),
    }


def require_trading_off(
    settings=Depends(get_effective_settings_dep),
) -> None:
    """FastAPI dependency: refuse a configuration change while trading is on.

    Applied per-ENDPOINT (never to a whole router, which would also block reads).
    """
    if is_trading_on(settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Trading is ON, so settings cannot be changed. Turn trading off "
                "first — a strategy must not be reconfigured while it is running."
            ),
        )
