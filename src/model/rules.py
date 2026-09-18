"""Strategy rules (rule-based signal model) — schema + JSON file persistence.

The rule set is the strategy artifact a human tunes while building the bot.
It is deliberately NOT stored as scalar ``.env`` keys: a rule set is a tree of
conditions that serializes cleanly to structured JSON. It lives in its own
file - the *store* (``data/strategies/store.json`` by default, path from
``STRATEGY_RULES_FILE``) - which holds EVERY strategy plus the name of the
active one, so it can be:

- edited by the Web Portal Rules panel (atomic write, same pattern as .env),
- read by the backtester and by ``simple_model.py`` through ONE loader, and
- versioned / copied per experiment without touching ``.env``.

Rules are the input for the rule-based generator (the only one there is, see
``simple_model.MODEL_KIND``). Example rule (all must hold): BUY when
``close < sma_50`` and ``rsi_14 < 30``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.config.settings import Settings

logger = logging.getLogger(__name__)

# Allowed comparison operators for a rule condition.
OPS: tuple = ("<", "<=", ">", ">=", "==", "!=", "crosses_above", "crosses_below")

# Sentinel used by the Rules panel when a condition compares against a number
# rather than another feature (ref).
VALUE_TARGET = "__value__"


class RuleCondition(BaseModel):
    """One predicate: ``feature <op> value`` OR ``feature <op> ref``."""

    model_config = ConfigDict(extra="ignore")

    feature: str = Field(..., min_length=1, description="Series to test (close/sma_50/rsi_14/...)")
    op: str = Field(default="<", description="One of OPS")
    value: Optional[float] = Field(default=None, description="Numeric threshold when not comparing to a feature")
    ref: Optional[str] = Field(default=None, description="Feature to compare against instead of a number")

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str) -> str:
        if v not in OPS:
            raise ValueError(f"op must be one of {', '.join(OPS)}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "RuleCondition":
        has_value = self.value is not None
        has_ref = self.ref is not None
        if has_value == has_ref:
            raise ValueError("a condition needs exactly one of 'value' (number) or 'ref' (feature)")
        if has_ref and self.ref == self.feature:
            raise ValueError(f"cannot compare {self.feature!r} to itself")
        return self


class Rule(BaseModel):
    """A BUY/SELL trigger made of one or more conditions."""

    model_config = ConfigDict(extra="ignore")

    side: Literal["BUY", "SELL"] = "BUY"
    mode: Literal["all", "any"] = Field(default="all", description="all conditions must hold, or any one")
    enabled: bool = Field(default=True, description="False keeps the rule but disables it")
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    conditions: List[RuleCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_at_least_one_condition(self) -> "Rule":
        if not self.conditions:
            raise ValueError("a rule needs at least one condition")
        return self


class RuleSet(BaseModel):
    """The whole rule-based strategy stored in the rules file."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="default")
    version: int = Field(default=1, ge=1)
    instrument: Optional[str] = Field(default=None, description="Symbol the rules were built for")
    description: str = Field(default="")
    updated_at: Optional[str] = None
    rules: List[Rule] = Field(default_factory=list)
    deleted: bool = Field(default=False, description="Soft-delete flag: the JSON entry is kept but hidden from the panel")
    config: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-strategy configuration (env KEY -> raw value) that overrides the global .env defaults",
    )


class StrategyStore(BaseModel):
    """The strategy store: one JSON object wrapping many named strategies.

    ``strategies`` maps a strategy name -> its ``RuleSet``; ``active`` names the
    strategy currently shown in the panel. Stored in the file referenced by
    ``STRATEGY_RULES_FILE`` (default ``data/strategies/store.json``). The file is
    local data - it is gitignored, and an absent file simply means an empty
    store (the portal then asks for a first strategy).
    """

    model_config = ConfigDict(extra="ignore")

    active: Optional[str] = None
    strategies: Dict[str, RuleSet] = Field(default_factory=dict)

    def live_strategies(self) -> Dict[str, RuleSet]:
        """Return only strategies that have NOT been soft-deleted."""
        return {name: rs for name, rs in self.strategies.items() if not rs.deleted}


# ---------------------------------------------------------------------------
# file I/O
# ---------------------------------------------------------------------------
def rules_file_path(settings: Settings) -> Path:
    """Path of the rules file (relative paths resolve against the project root)."""
    return Path(settings.strategy_rules_file)


def default_ruleset(instrument: Optional[str] = None) -> RuleSet:
    """A sensible starting rule set matching the plan's examples."""
    return RuleSet(
        name="default",
        instrument=instrument,
        description="Example mean-reversion rules — edit in the Rules panel.",
        rules=[
            Rule(
                side="BUY",
                mode="all",
                conditions=[
                    RuleCondition(feature="close", op="<", ref="sma_50"),
                    RuleCondition(feature="rsi_14", op="<", value=30.0),
                ],
            ),
            Rule(
                side="SELL",
                mode="all",
                conditions=[
                    RuleCondition(feature="close", op=">", ref="sma_20"),
                    RuleCondition(feature="rsi_14", op=">", value=70.0),
                ],
            ),
        ],
    )


def default_store(instrument: Optional[str] = None) -> StrategyStore:
    """A store with a single 'default' strategy (the example rules)."""
    rs = default_ruleset(instrument)
    return StrategyStore(active=rs.name, strategies={rs.name: rs})


def empty_strategy(name: str, instrument: Optional[str] = None) -> RuleSet:
    """A brand-new strategy with no rules yet (builder shows the empty state)."""
    return RuleSet(name=name, instrument=instrument, description="", rules=[])


# Settings keys that used to be stored per strategy and have been REPLACED by
# another key carrying the same meaning. Renamed keys are translated rather than
# dropped — a drop would silently change the strategy (a 5-year history window
# becoming the 2-year default) — and this is done on LOAD, so every reader (the
# panel, the effective-settings resolver, the backtester) sees one spelling and
# the next save writes only the new key.
RENAMED_CONFIG_KEYS = {
    # int years -> the ONE period value with its unit ("5" -> "5y")
    "HISTORICAL_LOOKBACK_YEARS": ("HISTORICAL_LOOKBACK", lambda v: f"{str(v).strip()}y"),
}


def migrate_config_keys(store: StrategyStore) -> StrategyStore:
    """Translate renamed per-strategy config keys in place; returns the store."""
    changed = False
    for rs in store.strategies.values():
        config = rs.config or {}
        for old, (new, convert) in RENAMED_CONFIG_KEYS.items():
            if old not in config:
                continue
            raw = str(config.pop(old)).strip()
            # Only fill the new key when it is unset: an explicit new value is
            # the operator's latest word on the subject and must win.
            if raw and not str(config.get(new, "")).strip():
                config[new] = convert(raw)
            changed = True
        rs.config = config
    if changed:
        logger.info("Translated renamed strategy config keys: %s", ", ".join(RENAMED_CONFIG_KEYS))
    return store


def normalize_active(store: StrategyStore) -> StrategyStore:
    """Ensure ``active`` points at a LIVE strategy (or None when none left)."""
    if not store.strategies:
        store.active = None
        return store
    live = [name for name, rs in store.strategies.items() if not rs.deleted]
    if not live:
        store.active = None
    elif not store.active or store.active not in live:
        store.active = live[0]
    return store


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rules.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_store(settings: Settings, store: StrategyStore) -> Path:
    """Validate + atomically persist the whole store, stamping the active one."""
    path = rules_file_path(settings)
    store = normalize_active(store)
    data = store.model_dump(exclude_none=False)
    active = data.get("active")
    if active and active in data["strategies"]:
        data["strategies"][active]["updated_at"] = _now()
    content = json.dumps(data, indent=2, default=str) + "\n"
    _atomic_write(path, content)
    logger.info("Saved %d strategy(ies) to %s (active=%s)", len(data["strategies"]), path, active)
    return path


def load_store(settings: Settings) -> StrategyStore:
    """Load the strategies file; returns an EMPTY store when none exists yet.

    The panel reacts to an empty store by showing the "create your first
    strategy" form. A legacy single-rule-set file (top-level ``rules``) is
    migrated into a ``default`` strategy and re-persisted in the new format.
    A corrupt file is left untouched (no data loss); the empty store is
    returned so the portal can still render (see ``store_error``).
    """
    path = rules_file_path(settings)
    if not path.exists():
        return StrategyStore()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "strategies" in raw:
            store = StrategyStore.model_validate(raw)
        elif isinstance(raw, dict) and "rules" in raw:
            # Legacy single-rule-set file -> wrap under its name / 'default'.
            rs = RuleSet.model_validate(raw)
            name = rs.name or "default"
            store = StrategyStore(active=name, strategies={name: rs})
            save_store(settings, store)
            logger.info("Migrated legacy rules file to strategy store format")
        else:
            return StrategyStore()
        return normalize_active(migrate_config_keys(store))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Rules file %s is invalid — starting empty: %s", path, exc)
        return StrategyStore()


def store_error(settings: Settings) -> Optional[str]:
    """Return a human message when the stored rules file is unreadable."""
    path = rules_file_path(settings)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "rules" in raw and "strategies" not in raw:
            RuleSet.model_validate(raw)  # legacy file, migrates on next load
        else:
            StrategyStore.model_validate(raw)
        return None
    except Exception as exc:  # noqa: BLE001 - any parse/validation failure
        return f"Rules file could not be parsed: {exc}"

