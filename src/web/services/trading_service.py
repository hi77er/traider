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

The state is runtime, not configuration — so it does not live in the strategy
store (which the lock itself would otherwise block), it lives beside the datasets
and is gitignored with them.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, status

from src.config.effective import active_strategy_name, get_effective_settings_dep
from src.execution.config import execution_status
from src.web.services import config_service

logger = logging.getLogger(__name__)

STATE_FILENAME = "trading.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(settings) -> Path:
    """``<data root>/trading.json`` — beside ``historical/`` and ``backtest_results/``."""
    return Path(settings.historical_data_dir).resolve().parent / STATE_FILENAME


def _read(settings) -> dict:
    """Current state, defaulting to OFF. Never raises: an unreadable file must
    not be mistaken for "on"."""
    path = state_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw["on"] = bool(raw.get("on"))
            return raw
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        logger.warning("Ignoring unreadable trading state at %s: %s", path, exc)
    return {"on": False, "since": None}


def _write(settings, state: dict) -> None:
    """Atomic write, owner-only like the account file (mkstemp gives 0600)."""
    path = state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trading.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    """Everything the Execution panel and the header dropdown need."""
    state = get_state(settings)
    return {
        "trading": state,
        "locked": bool(state.get("on")),
        "execution": execution_status(settings),
        "env_options": config_service.EXECUTION_ENV_OPTIONS,
        "strategy": active_strategy_name(),
        "instrument": settings.instrument,
        "bar_size": settings.historical_bar_size,
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
