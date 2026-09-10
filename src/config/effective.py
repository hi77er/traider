"""Effective-settings resolver: global .env defaults + the ACTIVE strategy's config.

The strategy JSON file (see ``STRATEGY_RULES_FILE``) is the source of truth for
everything that belongs to a strategy (instrument, bar size, features, model
thresholds, risk, rules). ``resolve_effective()`` returns a plain ``Settings``
object equal to the global ``.env`` settings OVERLAID with the active
strategy's ``config`` (env KEY -> raw string). Nothing is written to ``.env``.

Because every data/feature/chart module already consumes a ``Settings`` object,
this is the single seam that makes the whole bot (web, scheduler, backtest)
run "in the context of" the currently active strategy:

    base = Settings()                 # .env + schema defaults
    eff  = Settings(**active_config)  # strategy overrides those keys

Precedence: strategy config > .env > schema default. No active strategy (or
none of its keys present) -> plain global Settings.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from pydantic import ValidationError

from src.config.settings import Settings, get_settings
from src.model import rules as rules_mod

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Process-level cache of the resolved effective Settings. The cache is keyed by
# the mtimes of (.env, strategy file) so any change invalidates it implicitly;
# ``invalidate()`` forces the next call to re-resolve.
_cache: Optional[Settings] = None
_cache_key: Optional[tuple] = None
_env_sig: Optional[tuple] = None


def _field_by_env_key() -> Dict[str, str]:
    """Map ``ENV_KEY`` -> Settings field name (e.g. MODEL_BUY_THRESHOLD -> model_buy_threshold)."""
    return {name.upper(): name for name in Settings.model_fields}


def active_strategy_name() -> Optional[str]:
    """Name of the currently active (non-deleted) strategy, if any."""
    store = rules_mod.load_store(get_settings())
    name = store.active
    if not name or name not in store.strategies or store.strategies[name].deleted:
        return None
    return name


def strategy_config_for(name: Optional[str] = None) -> Dict[str, str]:
    """The stored ``config`` of the named (or active) strategy; empty when none."""
    store = rules_mod.load_store(get_settings())
    name = name or store.active
    if not name or name not in store.strategies or store.strategies[name].deleted:
        return {}
    return dict(store.strategies[name].config or {})


def resolve_effective(strategy_name: Optional[str] = None) -> Settings:
    """Return ``Settings`` = global .env overlaid with the strategy's config."""
    overlay = strategy_config_for(strategy_name)
    if not overlay:
        return Settings()  # no strategy context -> plain global settings

    field_map = _field_by_env_key()
    kwargs: Dict[str, str] = {}
    for key, raw in overlay.items():
        field = field_map.get(str(key).strip().upper())
        if field is None:
            logger.debug("Ignoring non-Settings key in strategy config: %s", key)
            continue
        kwargs[field] = str(raw).strip()

    if not kwargs:
        return Settings()
    if "instrument" in kwargs:
        kwargs["instrument"] = str(kwargs["instrument"]).strip().upper()
    try:
        eff = Settings(**kwargs)  # merges on top of .env + schema defaults
        logger.info("Effective settings resolved for strategy context")
        return eff
    except ValidationError as exc:
        logger.warning("Active strategy config invalid — falling back to global settings: %s", exc)
        return Settings()


def _file_signature(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _strategy_file_path() -> Path:
    return rules_mod.rules_file_path(get_settings())


def get_effective_settings() -> Settings:
    """Cached effective Settings; recomputed automatically when .env or the
    strategy file changes (mtime signature), or after ``invalidate()``."""
    global _cache, _cache_key, _env_sig

    env_path = _ENV_PATH
    current_env = _file_signature(env_path)
    if current_env != _env_sig:
        # .env changed (or first call): reload the cached global settings too.
        _env_sig = current_env
        get_settings.cache_clear()
        _cache = None

    store_sig = _file_signature(_strategy_file_path())
    key = (_env_sig, store_sig)
    if _cache is None or _cache_key != key:
        try:
            _cache = resolve_effective()
        except Exception:  # noqa: BLE001 - never let settings fail a request
            logger.exception("Failed to resolve effective settings")
            _cache = Settings()
        _cache_key = key
    return _cache


def invalidate() -> None:
    """Force the next ``get_effective_settings()`` call to re-resolve."""
    global _cache, _cache_key
    _cache = None
    _cache_key = None


def get_effective_settings_dep() -> Settings:
    """FastAPI dependency: inject the active strategy's effective Settings."""
    return get_effective_settings()
