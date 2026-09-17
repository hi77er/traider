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
on re-checks the credentials of the environment in play against the broker — every
time, both environments, whether or not a verdict is already on file (see
``src/execution/credentials.py``). Without it, "trading is ON" would be a promise
the bot cannot keep: the switch would sit there green while every order bounced off
a key that was revoked an hour ago. A failed check leaves trading OFF and says why;
an unreachable broker counts as failure, because a key we cannot prove is not a key
we can trade with.

The state is runtime, not configuration — so it does not live in the strategy
store (which the lock itself would otherwise block), it lives beside the datasets
and is gitignored with them.

The state itself now lives in ``src/config/trading_state.py``, because the
**execution loop** reads it too and must not have to import this package to ask.
This module keeps the policy and re-exports the state helpers, so every caller
that already imports them from here keeps working.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, status

from src.config.effective import active_strategy_name, get_effective_settings_dep
from src.config.trading_state import (
    STATE_FILENAME,  # noqa: F401  (re-exported for existing callers)
    get_state,
    is_trading_on,
    now_iso as _now_iso,
    state_path,  # noqa: F401  (re-exported for existing callers)
    write_state as _write,
)
from src.execution import credentials, positions
from src.execution.alpaca_client import AlpacaError
from src.execution.alpaca_executor import OrderRefused
from src.execution.config import ExecutionConfigError, execution_status
from src.web.services import config_service, freshness

logger = logging.getLogger(__name__)

__all__ = [
    "STATE_FILENAME",
    "flat_blocker",
    "get_state",
    "is_trading_on",
    "payload",
    "require_flat",
    "require_trading_off",
    "state_path",
    "stop_and_flatten",
    "turn_off",
    "turn_on",
]


# -- the exposure gate -----------------------------------------------------
def flat_blocker(settings, *, action: str, active_only: bool = False) -> Optional[str]:
    """Why this action must not proceed, or ``None`` when nothing is in the way.

    ``action`` leads the message because the same check guards four different things, and
    "Trading cannot start" is the wrong sentence for a strategy switch.

    It refuses on exactly two things: a position that is open, and an account that could
    not be read. Both are decisions not to trade blind — see
    ``src/execution/positions`` for why "no credentials" is provably flat while
    "unreachable" is not.

    ``active_only`` narrows the SECOND of those to the environment being traded, and it
    exists because a broken key for an account this screen is not pointed at is not a
    reason to freeze it. Changing the active strategy and deleting a strategy cannot strand
    a position in an account the bot is not trading: the danger there is a position in the
    account in play, whose flatten button would follow the switch. Refusing because the
    OTHER account could not be read blocked those actions permanently, with the remedy
    (fix or clear the other account's keys) nowhere in the message — a false positive that
    locked the picker for good.

    ⚠️ It does NOT narrow the held rule, and it must not: a position you can see, in either
    account, is exactly what the switch would leave behind. Arming (``turn_on``) and
    switching the environment keep the strict rule, because both of those DO reach an
    account whose contents are unknown.
    """
    states = positions.snapshots(settings)
    held = positions.held(states)
    blind = positions.unknown(states)
    if active_only:
        active = str(getattr(settings, "execution_env", "paper") or "paper").strip().lower()
        blind = [state for state in blind if state.env == active]

    if held:
        # The tail gives the operator both ways out, and both are real: flattening is
        # available in one click, and waiting works with no cleanup at all, because a
        # bracket that closes the position simply stops being there.
        return (
            f"{action} — {positions.describe(states)}. Flatten first "
            "(Stop trading & flatten), or wait: a resting bracket may close the position "
            "on its own."
        )
    if blind:
        why = "; ".join(p.reason for p in blind)
        return (
            f"{action} — {why}, so what the account holds is unknown. Refusing rather "
            "than assuming it is flat."
        )
    return None


def require_flat(action: str, *, active_only: bool = False):
    """A FastAPI dependency refusing ``action`` while a position is open.

    A factory rather than a plain function so each route can say what it is refusing —
    the operator should read "the environment cannot be switched" and not a generic
    "conflict".

    This one asks the BROKER, so unlike ``require_trading_off`` (cheap, local, and on the
    frequent settings writes) it guards only the rare user-initiated actions that can
    strand a position. The lock follows EXPOSURE, not the switch: it is holding something
    that matters, not whether trading is armed.

    ``active_only`` is passed on to :func:`flat_blocker`: true for the actions that cannot
    strand a position in an account they do not touch (changing or deleting a strategy),
    false for the ones that can (arming, changing the environment).
    """

    def dependency(settings=Depends(get_effective_settings_dep)) -> None:
        blocker = flat_blocker(settings, action=action, active_only=active_only)
        if blocker:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blocker)

    return dependency


def turn_on(settings, *, confirm_live: bool = False) -> dict:
    """Start trading, or explain why it cannot. Never raises."""
    # FIRST, before the credentials and before the confirmation: a position that is
    # already open. Arming cannot repair that, and the operator needs to hear about the
    # POSITION rather than about a key — it is the actionable problem, and the one they
    # probably do not know about.
    #
    # This is the deliberate decision that makes "only the active strategy trades" true of
    # the POSITION as well as of new entries: the bot will not start on top of something
    # it did not open, because then nothing on screen would own it.
    blocker = flat_blocker(settings, action="Trading cannot start")
    if blocker:
        return {
            "ok": False,
            "needs_flatten": True,
            "message": blocker,
            "state": get_state(settings),
            "execution": execution_status(settings),
            "positions": [p.as_dict() for p in positions.snapshots(settings)],
        }

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

    # Configured ≠ working, and "worked yesterday" ≠ works now: the switch is the
    # last gate before orders exist, so the credentials in play are checked against
    # the broker AT THIS MOMENT, whatever the stored verdict says. A key can be
    # revoked, rotated, or belong to a restricted account, and none of that changes
    # the fingerprint — so a cached pass is not evidence about right now.
    #
    # Both environments, because the same thing is at stake in both: an order the
    # broker will refuse (a paper order is still a broken setup the operator should
    # hear about, and the same code path runs live). `force=True` is what makes this
    # a fresh call rather than a lookup; the verdict it reaches is recorded, so the
    # popup and the header agree with the decision that was just made.
    #
    # Unreachable counts as failure here — fail closed. If we cannot prove the key
    # works, we do not claim the bot is trading.
    env = status_info["env"]
    check = credentials.verify(settings, env, force=True)
    if not check["ok"]:
        # The gate above already refused a half-configured pair, so the check either
        # reached Alpaca or could not be made at all — both come back as a message.
        reason = check["message"]
        logger.warning("TRADING REFUSED: %s credentials failed revalidation — %s", env.upper(), reason)
        return {
            "ok": False,
            "needs_verification": True,
            "message": f"Trading cannot start — {reason}",
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
        # WHICH strategy this arming applies to. The loop trades the STAMPED strategy and
        # refuses on a mismatch, so a stale tab, a hand-edited store or a future endpoint
        # that forgets the lock produces a loud refusal instead of orders for the wrong
        # strategy. The name is frozen here rather than looked up per tick, because "the
        # active one" is a question whose answer can change while trading is on — which
        # is the reason to record it.
        "strategy": active_strategy_name(),
    }
    _write(settings, state)
    logger.warning("TRADING TURNED ON (%s, %s, strategy %s)", state["env"].upper(), state["broker"], state["strategy"])
    result = {
        "ok": True,
        "message": f"Trading is ON — orders route to {state['env'].upper()}",
        "state": state,
        "execution": status_info,
    }
    # The gate may have been served from the cache a moment ago; an arming is exactly the
    # point at which a stale view of the account stops being acceptable.
    positions.forget()
    return result


def turn_off(settings) -> dict:
    """Stop trading and release the configuration lock.

    **ALWAYS allowed, and unconditionally.** It is the stop button: no position needs
    flattening, no broker needs reaching, and no credential needs to be valid. A broker
    outage must not be able to trap the bot in the ON state, so this function makes no
    call that could fail before the state is written.
    """
    previous = get_state(settings)
    state = {
        "on": False,
        "since": None,
        "stopped_at": _now_iso(),
        "env": previous.get("env"),
        "broker": previous.get("broker"),
        "base_url": previous.get("base_url"),
        # Kept so the record still says WHAT was running, which is the first thing anyone
        # asks afterwards.
        "strategy": previous.get("strategy"),
    }
    _write(settings, state)
    logger.warning("TRADING TURNED OFF — configuration is unlocked again")

    # Read AFTER the state is written, so a broker that hangs or fails delays the report
    # and never the stop. This is the one place the order matters more than the answer.
    status_info = execution_status(settings)
    left = _left_behind(settings, status_info["env"])
    return {
        "ok": True,
        "message": "Trading is OFF — settings can be changed again" + _left_suffix(left),
        "state": state,
        "execution": status_info,
        "left": left,
    }


def _left_behind(settings, env: str) -> Dict[str, Any]:
    """What turning trading off leaves open, and whether it is protected.

    OFF does not close anything, so the copy has to say so — and it has to distinguish the
    three cases, which are not equally bad:

    * nothing open — nothing to do;
    * open with a resting exit — the bracket will close it, so this is a "wait";
    * open with NO exit — the loud one. A position with no stop is unmanaged the moment it
      exists, and an empty ``STOP_LOSS_PERCENT`` is a perfectly ordinary way to be in it.
    """
    report: Dict[str, Any] = {
        "env": env,
        "open": 0,
        "protected": True,
        "unprotected": [],
        "known": True,
        "positions": [],
        "message": "no positions are open",
    }
    state = positions.snapshot(settings, env, force=True)
    report["known"] = state.known
    report["open"] = state.count
    report["positions"] = state.as_dict()["positions"]
    if not state.known:
        report["protected"] = False
        report["message"] = f"the {env} account could not be read ({state.reason}) — what it holds is unknown"
        return report
    if not state.payloads:
        return report

    unprotected: list = []
    try:
        executor = positions.executor_for(settings, env)
        for payload in state.payloads:
            symbol = str(payload.get("symbol") or "").upper()
            if symbol and not executor.resting_exits(symbol):
                unprotected.append(symbol)
    except (ExecutionConfigError, AlpacaError, OrderRefused) as exc:
        report["protected"] = False
        report["message"] = f"{state.count} position(s) are STILL OPEN and the exits could not be read ({exc})"
        report["unprotected"] = [str(p.get("symbol") or "").upper() for p in state.payloads]
        return report

    report["unprotected"] = unprotected
    report["protected"] = not unprotected
    if unprotected:
        report["message"] = (
            f"{state.count} position(s) are STILL OPEN and {', '.join(unprotected)} has no "
            "resting exit — it is unmanaged until you flatten it"
        )
    else:
        report["message"] = (
            f"{state.count} position(s) are STILL OPEN, each with a resting exit — the "
            "bracket should close them on its own"
        )
    return report


def _left_suffix(left: Dict[str, Any]) -> str:
    """The one line appended to the stop message, loudest case first."""
    if not left.get("open"):
        return ""
    if not left.get("known"):
        return " — but the account could not be read, so check it"
    if left.get("unprotected"):
        return f" — {left['open']} position(s) are STILL OPEN with NO exit; flatten them"
    return f" — {left['open']} position(s) are still open with a resting exit"


def stop_and_flatten(settings, *, confirm_live: bool = False) -> dict:
    """Stop trading AND close what is open, as one deliberate action.

    Two different wishes that used to be one button: "stop" and "I do not want an
    unmanaged position". OFF alone does not close anything, so this is the verb that
    covers the second — and it is separate from ``turn_off`` precisely because that one
    must stay clickable when the broker is down.

    **The order is not negotiable:** trading goes OFF first, unconditionally and without
    touching the network. A flatten that fails therefore leaves the bot STOPPED, which is
    the opposite of what would happen if the two steps ran the other way round.
    """
    status_info = execution_status(settings)
    if status_info["live"] and not confirm_live:
        return {
            "ok": False,
            "needs_live_confirmation": True,
            "message": (
                "This will close the position in the LIVE account, with real money. "
                "Confirm to stop trading and flatten."
            ),
            "execution": status_info,
        }

    stopped = turn_off(settings)
    env = status_info["env"]

    try:
        executor = positions.executor_for(settings, env)
    except ExecutionConfigError as exc:
        positions.forget()
        return {
            "ok": False,
            "message": f"Trading is OFF — but nothing could be flattened ({exc})",
            "trading": stopped["state"],
            "env": env,
            "flattened": [],
        }

    fresh = positions.snapshot(settings, env, force=True)
    results: list = []
    for payload in fresh.payloads:
        symbol = str(payload.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            results.append({"symbol": symbol, "result": executor.flatten(symbol, "stop & flatten")})
        except (OrderRefused, AlpacaError) as exc:
            # Reported, never raised: the bot is already stopped, and swallowing the rest
            # of the list would leave the other positions unmentioned.
            logger.error("Could not flatten %s: %s", symbol, exc)
            results.append({"symbol": symbol, "error": str(exc)})

    positions.forget()
    failed = [r for r in results if r.get("error")]
    if not fresh.known:
        message = f"Trading is OFF — but the {env} account could not be read, so nothing was flattened"
    elif failed:
        message = f"Trading is OFF, but {len(failed)} position(s) could not be closed — see the log"
    elif results:
        message = f"Trading is OFF and {len(results)} position(s) were closed"
    else:
        message = "Trading is OFF — there was nothing open to flatten"
    return {
        "ok": not failed and fresh.known,
        "message": message,
        "trading": stopped["state"],
        "env": env,
        "flattened": results,
    }


def payload(settings) -> dict:
    """Everything the header controls and the armed panel need."""
    state = get_state(settings)
    status_info = execution_status(settings)
    # What is OPEN, which is not the same question as whether trading is armed. The panel
    # needs the first to stay on screen in the second case: "off" is not "flat", and a
    # position held while OFF is the one moment the operator most needs the flatten button.
    # One cached read per account, not one per request — see src/execution/positions.
    accounts = positions.snapshots(settings)
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
        # ...and whether this process is still the gate those files describe. A server
        # that quietly serves last week's rules is worse than a stale button, because
        # the thing it would get wrong is whether real orders may start.
        "freshness": freshness.info(),
        "positions": [p.as_dict() for p in accounts],
        "open_count": positions.total(accounts),
        "unknown_count": len(positions.unknown(accounts)),
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
