"""Strategy Rules + selection API endpoints for the Web Portal.

The rules file stores MANY named strategies (``{active, strategies}``). The
whole dashboard is strategy-scoped, so all reads use the *effective* settings
(the active strategy's config overlaid on .env). Creating / selecting /
deleting / saving a strategy updates the JSON, after which the page reloads
and every panel renders in the new strategy's context.
"""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.web.auth import require_auth
from src.web.services import rules_service
from src.web.services import trading_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/rules",
    tags=["rules"],
    dependencies=[Depends(require_auth)],
)


class StrategyCreate(BaseModel):
    """Create a new (empty) strategy by name."""

    name: str


class StrategySave(BaseModel):
    """Save the rules of one named strategy (creates it if missing)."""

    name: str
    ruleset: Dict  # strict model validation happens in the service


class StrategyRename(BaseModel):
    """Rename an existing strategy to a new, unique name."""

    name: str
    new_name: str


class StrategyDelete(BaseModel):
    """Delete a strategy, optionally removing its dataset file(s) too."""

    name: str
    delete_data: bool = False


@router.get("")
def get_rules(
    default: bool = False, settings: Settings = Depends(get_effective_settings_dep)
) -> dict:
    """Return the strategy store + builder metadata (series, ops) in context."""
    return rules_service.payload(settings, default=default)


@router.post("/create", dependencies=[Depends(require_trading_off)])
def create_strategy(body: StrategyCreate, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Create (or select) a strategy and switch the active one to it."""
    return rules_service.create_strategy(settings, body.name)


@router.post(
    "/select",
    dependencies=[
        Depends(require_trading_off),
        # Selecting a DIFFERENT strategy while a position is open is refused because the
        # flatten that follows would run against the NEW strategy's instrument and
        # environment, and miss the position entirely. Blocking the switch is what keeps
        # "flatten first" reachable from the screen where the position is actually visible.
        #
        # ``active_only``: the position that matters is one in the account BEING TRADED. A
        # key for the other account that the broker rejects is not a reason to freeze the
        # picker — that account is not touched by a switch, and refusing for it locked
        # strategy changes permanently with the remedy nowhere in the message.
        Depends(trading_service.require_flat(
            "The active strategy cannot be changed while a position is open", active_only=True,
        )),
    ],
)
def select_strategy(body: StrategyCreate, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Switch the active strategy to an existing one (same as create-if-exists)."""
    return rules_service.create_strategy(settings, body.name)


@router.post(
    "/delete",
    dependencies=[
        Depends(require_trading_off),
        # Deleting the strategy that owns a position would leave nothing that knows how to
        # close it — the instrument and the environment go with the strategy. ``active_only``
        # for the same reason as the switch: a position in the account being traded is the
        # one that would be abandoned, and an unreadable account elsewhere is not.
        Depends(trading_service.require_flat(
            "A strategy cannot be deleted while a position is open", active_only=True,
        )),
    ],
)
def delete_strategy(body: StrategyDelete, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Soft-delete a named strategy (kept in the file, hidden from the panel).
    When ``delete_data`` is set the instrument's dataset file(s) are removed too
    (unless still referenced by another live strategy)."""
    return rules_service.delete_strategy(settings, body.name, delete_data=body.delete_data)


@router.post("/rename", dependencies=[Depends(require_trading_off)])
def rename_strategy(body: StrategyRename, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Rename a strategy (JSON key + name); the active pointer follows if renamed."""
    return rules_service.rename_strategy(settings, body.name, body.new_name)


@router.post("", dependencies=[Depends(require_trading_off)])
def save_strategy(body: StrategySave, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Validate + save the submitted rules/config under the named strategy.

    Refused while trading is on: the panel's Save buttons are disabled then too,
    but the rule has to hold for a direct POST as well.
    """
    return rules_service.update_strategy(settings, body.name, body.ruleset)


@router.post("/reset", dependencies=[Depends(require_trading_off)])
def reset_rules(settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Reset the store to a single 'default' example strategy."""
    return rules_service.reset(settings)
