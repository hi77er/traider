"""Strategy Rules business logic for the Web Portal (Rules panel).

Wraps ``src.model.rules`` (schema + JSON file persistence) with the pieces the
UI needs: the list of series/features the rule builder may reference, the
available comparison operators, and validated create/save/reset operations.

The file is a *strategy store*: ``{active, strategies: {name: RuleSet}}``, so
the panel can switch between named strategies and store many of them in one
JSON document.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pydantic import ValidationError

from src.config.settings import Settings
from src.features.schema import allowed_series
from src.model import rules as rules_mod
from src.web.services import config_service

logger = logging.getLogger(__name__)

# Hard cap on how many LIVE (non-deleted) strategies the panel may hold.
MAX_STRATEGIES = 5


def allowed_features(settings: Settings):
    """Ordered series a rule can test against: raw OHLCV + active features."""
    return list(allowed_series(settings))


def _payload_dict(settings: Settings, store: rules_mod.StrategyStore, error: Optional[str]) -> dict:
    path = rules_mod.rules_file_path(settings)
    # Per-strategy configuration schema (values fall back to global defaults).
    # Risk gets its own panel, so it is split out here rather than rendered
    # twice — a key can only be edited in one place.
    config_groups, risk_groups = config_service.split_strategy_groups(
        config_service.strategy_config_groups(settings)
    )
    return {
        "file": str(path),
        "file_exists": path.exists(),
        "allowed_features": allowed_features(settings),
        "ops": list(rules_mod.OPS),
        "active": store.active,
        "max_strategies": MAX_STRATEGIES,
        # Only live (non-deleted) strategies are shown to the panel.
        "strategies": {name: rs.model_dump() for name, rs in store.live_strategies().items()},
        # Per-strategy configuration schema (values fall back to global defaults).
        "config_groups": config_groups,
        "risk_groups": risk_groups,
        "error": error,
    }


def payload(settings: Settings, *, default: bool = False) -> dict:
    """GET payload: the strategy store + everything the builder needs."""
    if default:
        return _payload_dict(settings, rules_mod.default_store(settings.instrument), None)
    store = rules_mod.load_store(settings)
    return _payload_dict(settings, store, rules_mod.store_error(settings))


def create_strategy(settings: Settings, name: str) -> dict:
    """Create a new empty strategy and switch the active one to it."""
    path = rules_mod.rules_file_path(settings)
    name = (name or "").strip()
    if not name:
        return {"ok": False, "message": "Strategy name cannot be empty", "errors": [], "file": str(path)}

    store = rules_mod.load_store(settings)
    live = store.live_strategies()
    if name not in store.strategies and len(live) >= MAX_STRATEGIES:
        return {
            "ok": False,
            "message": f"Strategy limit reached ({MAX_STRATEGIES}) — delete one to create another.",
            "errors": [],
            "file": str(path),
        }
    if name in store.strategies:
        if store.strategies[name].deleted and len(live) >= MAX_STRATEGIES:
            return {
                "ok": False,
                "message": f"Strategy limit reached ({MAX_STRATEGIES}) — delete one to restore “{name}”.",
                "errors": [],
                "file": str(path),
            }
        # Already exists (possibly soft-deleted earlier): resurrect it + switch.
        store.strategies[name].deleted = False
        store.active = name
        rules_mod.save_store(settings, store)
        logger.info("Switched to strategy %r", name)
        return {"ok": True, "message": f"Switched to strategy “{name}”", "errors": [], "file": str(path),
                "payload": _payload_dict(settings, store, rules_mod.store_error(settings))}

    rs = rules_mod.empty_strategy(name, settings.instrument)
    # Seed the new strategy with the current global values so its config is
    # a full, editable snapshot from the very first save.
    rs.config = config_service.strategy_config_defaults(settings)
    rs.instrument = rs.config.get("INSTRUMENT") or settings.instrument
    store.strategies[name] = rs
    store.active = name
    rules_mod.save_store(settings, store)
    logger.info("Created strategy %r", name)
    return {"ok": True, "message": f"Strategy “{name}” created", "errors": [], "file": str(path),
            "payload": _payload_dict(settings, store, rules_mod.store_error(settings))}


def delete_strategy(settings: Settings, name: str, delete_data: bool = False) -> dict:
    """Soft-delete a strategy: flag it ``deleted`` but keep the JSON entry.

    The strategy disappears from the panel; if it was the active one the
    panel switches to the next live strategy (or shows the create form when
    none are left).

    When ``delete_data`` is true the strategy's canonical dataset file(s) are
    also deleted — but only the ones no other LIVE strategy still references.
    A dataset file is keyed by instrument AND bar size, so the reference check is
    per (instrument, bar size) pair: a strategy on ``NVDA`` 1-minute bars keeps
    ``NVDA_1m.parquet`` alive, and must NOT keep ``NVDA_1d.parquet`` alive for a
    sibling that has moved on to minute bars. ``skipped`` explains the pairs that
    were kept because something else is still using them.
    """
    path = rules_mod.rules_file_path(settings)
    name = (name or "").strip()
    store = rules_mod.load_store(settings)
    if name not in store.strategies:
        return {"ok": False, "message": f"No strategy “{name}” to delete", "errors": [], "file": str(path)}
    if store.strategies[name].deleted:
        return {"ok": False, "message": f"Strategy “{name}” is already deleted", "errors": [], "file": str(path)}

    rs = store.strategies[name]
    store.strategies[name].deleted = True
    if store.active == name:
        store.active = None
    rules_mod.save_store(settings, store)  # normalize_active picks next live strategy

    removed: List[str] = []
    skipped: Optional[str] = None
    if delete_data:
        symbol = _strategy_instrument(rs)
        if not symbol:
            skipped = "no instrument recorded for this strategy — no data deleted"
        else:
            # (instrument, bar size) pairs the OTHER live strategies are pinned to.
            # Everything else under this instrument is fair game: that covers the
            # strategy's own file and the files it left behind when it changed bar
            # size, while a file any live strategy still reads survives.
            users_by_bar: Dict[str, List[str]] = {}
            for who, other in store.live_strategies().items():
                if who == name or _strategy_instrument(other) != symbol:
                    continue
                users_by_bar.setdefault(_strategy_bar_size(other, settings), []).append(who)
            removed, kept = _delete_symbol_datasets(
                settings, symbol, keep_intervals=set(users_by_bar)
            )
            if not removed:
                if kept:
                    who = ", ".join(
                        f"{', '.join(sorted(users_by_bar[bar]))} ({bar})" for bar in sorted(kept)
                    )
                    skipped = f"{symbol} dataset kept — still used by: {who}"
                else:
                    skipped = f"no dataset file found for {symbol}"

    return {
        "ok": True,
        "message": f"Strategy “{name}” deleted",
        "errors": [],
        "file": str(path),
        "removed": removed,
        "skipped": skipped,
        "payload": _payload_dict(settings, store, rules_mod.store_error(settings)),
    }


def _strategy_instrument(rs) -> str:
    """A strategy's instrument, upper-cased ('' when it records none)."""
    raw = rs.instrument or (rs.config or {}).get("INSTRUMENT", "")
    return str(raw or "").strip().upper()


def _strategy_bar_size(rs, settings: Settings) -> str:
    """A strategy's bar size; the global setting is the fallback.

    ``HISTORICAL_BAR_SIZE`` is per-strategy, and a store written before that was
    true (or a strategy created before the key existed) has no value of its own —
    such a strategy runs on the global default, so that is what its dataset file
    is named after.
    """
    raw = (rs.config or {}).get("HISTORICAL_BAR_SIZE")
    return str(raw or "").strip() or str(getattr(settings, "historical_bar_size", "") or "")


def _delete_symbol_datasets(
    settings: Settings, symbol: str, keep_intervals: Optional[set] = None
) -> tuple:
    """Delete ``symbol``'s canonical parquet datasets except the kept intervals.

    ``keep_intervals`` are the bar sizes a live strategy still references (any
    strategy, not just this one). Returns ``(removed, kept)``: the deleted
    ``SYMBOL_INTERVAL`` keys, and the intervals that survived for that reason.
    """
    from pathlib import Path

    from src.data.dataset import delete_dataset as _delete_dataset

    removed: List[str] = []
    kept: List[str] = []
    keep = {str(i) for i in (keep_intervals or set())}
    data_dir = Path(settings.historical_data_dir)
    if not data_dir.is_dir():
        return removed, kept
    prefix = f"{symbol}_"
    files = sorted(p for p in data_dir.iterdir() if p.is_file() and p.name.startswith(prefix) and p.suffix == ".parquet")
    for path in files:
        interval = path.name[len(prefix):-len(".parquet")]
        if not interval:
            continue
        if interval in keep:
            kept.append(interval)
            continue
        try:
            if _delete_dataset(settings, symbol, interval):
                removed.append(f"{symbol}_{interval}")
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't abort the rest
            logger.warning("Could not delete dataset %s: %s", path, exc)
    return removed, kept


def rename_strategy(settings: Settings, name: str, new_name: str) -> dict:
    """Rename a strategy: both its JSON key and ``RuleSet.name`` become ``new_name``.

    The active pointer follows the rename when the renamed strategy is the
    active one, and the renamed entry keeps its position in the file. The new
    name must be unique among LIVE strategies; a name held only by a
    soft-deleted strategy is free to claim (the invisible deleted entry is
    permanently removed — its content is gone anyway and can no longer be
    resurrected under that name).
    """
    path = rules_mod.rules_file_path(settings)
    name = (name or "").strip()
    new_name = (new_name or "").strip()
    if not name or not new_name:
        return {"ok": False, "message": "Both the current and the new strategy name are required",
                "errors": [], "file": str(path)}
    if new_name == name:
        return {"ok": False, "message": "The new name is the same as the current name",
                "errors": [], "file": str(path)}

    store = rules_mod.load_store(settings)
    if name not in store.strategies or store.strategies[name].deleted:
        return {"ok": False, "message": f"No live strategy “{name}” to rename",
                "errors": [], "file": str(path)}
    existing = store.strategies.get(new_name)
    if existing is not None and not existing.deleted:
        return {"ok": False, "message": f"A strategy “{new_name}” already exists",
                "errors": [], "file": str(path)}

    rs = store.strategies[name].model_copy(update={"name": new_name})
    # Rebuild the dict so the renamed strategy keeps its place in the list and
    # the soft-deleted occupant of ``new_name`` (if any) is dropped, then move
    # the active pointer if we renamed the active strategy.
    strategies = {}
    for key, item in store.strategies.items():
        if key == name:
            strategies[new_name] = rs
        elif key != new_name:
            strategies[key] = item
    store.strategies = strategies
    if store.active == name:
        store.active = new_name
    rules_mod.save_store(settings, store)
    claimed = existing is not None  # a soft-deleted entry was taken over
    logger.info("Renamed strategy %r -> %r%s", name, new_name, " (claimed deleted name)" if claimed else "")
    return {"ok": True,
            "message": (f"Strategy “{name}” renamed to “{new_name}”"
                        + (" (took over a previously deleted strategy)" if claimed else "")),
            "errors": [],
            "file": str(path),
            "payload": _payload_dict(settings, store, rules_mod.store_error(settings))}


def update_strategy(settings: Settings, name: str, ruleset: dict) -> dict:
    """Validate + persist the rules of one named strategy (creates if missing)."""
    path = rules_mod.rules_file_path(settings)
    name = (name or "").strip()
    if not name:
        return {"ok": False, "message": "Strategy name cannot be empty", "errors": [], "file": str(path)}

    # Keys that no longer exist are dropped BEFORE validation, not after: `config`
    # is Dict[str, str], and a retired key could hold a non-string (the old
    # EXECUTION_LIVE_ACK was a bool), which pydantic would reject — leaving a
    # strategy saved before the removal impossible to save ever again.
    incoming = dict(ruleset or {})
    incoming_config = incoming.get("config")
    if isinstance(incoming_config, dict):
        incoming["config"] = {
            k: v for k, v in incoming_config.items()
            if k not in config_service.RETIRED_STRATEGY_KEYS
        }
    try:
        rs = rules_mod.RuleSet.model_validate(incoming)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        logger.warning("Rules update rejected for %r: %s", name, errors)
        return {"ok": False, "message": "Invalid rules", "errors": errors, "file": str(path)}

    # Per-strategy config values are validated against the real Settings model.
    if rs.config:
        ok, errors = config_service.validate_strategy_config(rs.config)
        if not ok:
            logger.warning("Strategy config rejected for %r: %s", name, errors)
            return {"ok": False, "message": "Invalid strategy configuration", "errors": errors, "file": str(path)}

    store = rules_mod.load_store(settings)
    rs = rs.model_copy(update={"name": name})  # file key is the source of truth
    # Keys the strategy panel does not render must survive a panel save.
    # EXECUTION_ENV is owned by the header dropdown, so without this a plain
    # "Save" on the configuration panel would silently reset a LIVE strategy
    # back to paper.
    existing = store.strategies.get(name)
    if existing is not None:
        preserved = {
            k: v for k, v in (existing.config or {}).items()
            if k in config_service.PANEL_HIDDEN_STRATEGY_KEYS
        }
        if preserved:
            rs = rs.model_copy(update={"config": {**preserved, **rs.config}})
    if rs.config.get("INSTRUMENT"):
        cfg = dict(rs.config)
        cfg["INSTRUMENT"] = str(cfg["INSTRUMENT"]).strip().upper()
        rs = rs.model_copy(update={"instrument": cfg["INSTRUMENT"], "config": cfg})
    store.strategies[name] = rs
    store.active = name
    rules_mod.save_store(settings, store)
    return {"ok": True,
            "message": f"Saved {len(rs.rules)} rule(s) to strategy “{name}”",
            "errors": [],
            "file": str(path),
            "payload": _payload_dict(settings, store, rules_mod.store_error(settings))}


def set_execution_env(settings: Settings, env: str) -> dict:
    """Point the ACTIVE strategy's orders at ``paper`` or ``live``.

    Deliberately not part of the strategy panel: the environment is chosen from
    the header dropdown, so it gets its own narrow write path instead of being
    smuggled through a full ruleset save. The value still goes through
    ``Settings`` validation, so only 'paper' and 'live' are ever stored.
    """
    path = rules_mod.rules_file_path(settings)
    store = rules_mod.load_store(settings)
    name = store.active
    if not name or name not in store.strategies or store.strategies[name].deleted:
        return {"ok": False, "message": "No active strategy", "errors": [], "file": str(path)}

    value = str(env or "").strip().lower() or "paper"
    ok, errors = config_service.validate_strategy_config({"EXECUTION_ENV": value})
    if not ok:
        logger.warning("Execution environment rejected for %r: %s", name, errors)
        return {"ok": False, "message": "Invalid environment", "errors": errors, "file": str(path)}

    rs = store.strategies[name]
    rs.config = {**(rs.config or {}), "EXECUTION_ENV": value}
    rules_mod.save_store(settings, store)
    return {
        "ok": True,
        "message": f"“{name}” now routes orders to {value.upper()}",
        "errors": [],
        "file": str(path),
        "environment": value,
    }


def reset(settings: Settings) -> dict:
    """Reset ONLY the ACTIVE strategy's rules to the default example.

    This is deliberately NON-destructive to strategies: every other strategy is
    left untouched, and the active strategy keeps its name, instrument and
    per-strategy configuration — only its ``rules`` are replaced.
    """
    path = rules_mod.rules_file_path(settings)
    store = rules_mod.load_store(settings)
    active = store.active
    if not active or active not in store.strategies or store.strategies[active].deleted:
        return {"ok": False, "message": "No active strategy to reset", "errors": [], "file": str(path)}

    current = store.strategies[active]
    example = rules_mod.default_ruleset(current.instrument or settings.instrument)
    example = example.model_copy(
        update={
            "name": current.name,
            "instrument": current.instrument,
            "description": current.description or example.description,
            "config": dict(current.config or {}),
            "deleted": False,
        }
    )
    store.strategies[active] = example
    rules_mod.save_store(settings, store)
    return {"ok": True,
            "message": f"Reset rules of “{active}” to the default example",
            "errors": [],
            "file": str(path),
            "payload": _payload_dict(settings, store, rules_mod.store_error(settings))}

