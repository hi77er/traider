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


@router.post("/create")
def create_strategy(body: StrategyCreate, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Create (or select) a strategy and switch the active one to it."""
    return rules_service.create_strategy(settings, body.name)


@router.post("/select")
def select_strategy(body: StrategyCreate, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Switch the active strategy to an existing one (same as create-if-exists)."""
    return rules_service.create_strategy(settings, body.name)


@router.post("/delete")
def delete_strategy(body: StrategyDelete, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Soft-delete a named strategy (kept in the file, hidden from the panel).
    When ``delete_data`` is set the instrument's dataset file(s) are removed too
    (unless still referenced by another live strategy)."""
    return rules_service.delete_strategy(settings, body.name, delete_data=body.delete_data)


@router.post("/rename")
def rename_strategy(body: StrategyRename, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Rename a strategy (JSON key + name); the active pointer follows if renamed."""
    return rules_service.rename_strategy(settings, body.name, body.new_name)


@router.post("")
def save_strategy(body: StrategySave, settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Validate + save the submitted rules/config under the named strategy."""
    return rules_service.update_strategy(settings, body.name, body.ruleset)


@router.post("/reset")
def reset_rules(settings: Settings = Depends(get_effective_settings_dep)) -> dict:
    """Reset the store to a single 'default' example strategy."""
    return rules_service.reset(settings)
