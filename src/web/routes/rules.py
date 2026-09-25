"""Strategy Rules + selection API endpoints for the Web Portal.

The rules file stores MANY named strategies (``{active, strategies}``). The
whole page is strategy-scoped, so all reads use the *effective* settings
(the active strategy's config overlaid on .env). Creating / selecting /
deleting / saving a strategy updates the JSON, after which the page reloads
and every panel renders in the new strategy's context.
"""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.config import effective
from src.config.effective import get_effective_settings_dep
from src.config.settings import Settings
from src.web.services import rules_service
from src.web.services import trading_service
from src.web.services.trading_service import require_trading_off

router = APIRouter(
    prefix="/api/v1/rules",
    tags=["rules"],
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
        # Selecting a DIFFERENT strategy while a position is open used to be refused outright,
        # because the flatten that follows would run against the NEW strategy's instrument and
        # environment and miss the position entirely.
        #
        # The check now lives in the handler instead, because the answer depends on WHICH
        # strategy is being selected: handing the run to the position's OWNER — the strategy
        # whose instrument the open position is in, in the account this run trades — is the one
        # way through that keeps the position, and it has to be reachable. Without it the
        # operator could neither arm the strategy holding the position nor select it, so their
        # only options were to close the position by hand or to leave it open with nothing
        # managing it. Selecting anything else still refuses; the position's owner takes it over
        # and can then only close it (see ``LiveDriver.adopt_broker_position``).
    ],
)
def select_strategy(body: StrategyCreate, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Switch the active strategy to an existing one (same as create-if-exists)."""
    # The strategy being selected, resolved to the instrument IT trades: that is what decides
    # whether the open position is one it could take over.
    environment = str(getattr(settings, "execution_env", "paper") or "paper").lower()
    blocker = trading_service.flat_blocker(
        settings,
        action="The active strategy cannot be changed while a position is open",
        unreadable_blocks=False,
        trade_env=environment,
        held_ok_in_account=environment,
        held_ok_symbol=trading_service.instrument_of(settings, body.name),
    )
    if blocker:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blocker)
    return rules_service.create_strategy(settings, body.name)


@router.post(
    "/delete",
    dependencies=[
        Depends(require_trading_off),
        # Deleting the strategy that owns a position would leave nothing that knows how to
        # close it — the instrument and the environment go with the strategy.
        # ``unreadable_blocks=False`` for the same reason as the switch: a rejected key for
        # an account we cannot see into is not evidence of a position, and it is not
        # something the delete can fix or worsen.
        Depends(trading_service.require_flat(
            "A strategy cannot be deleted while a position is open",
            unreadable_blocks=False,
        )),
    ],
)
def delete_strategy(body: StrategyDelete, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Soft-delete a named strategy (kept in the file, hidden from the panel).
    When ``delete_data`` is set the strategy instrument's dataset file(s) are
    removed too — except the ones another live strategy still references, which
    is decided per (instrument, bar size) pair."""
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
