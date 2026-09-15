"""Effective-settings resolver: .env defaults + the account file + the ACTIVE strategy.

Three layers are merged into the ONE ``Settings`` object every module consumes:

| Layer | File | Example keys |
|-------|------|--------------|
| Global | ``.env`` | ``OPENBB_PROVIDER``, ``OPENBB_API_KEY`` |
| Account | ``settings/account/account.json`` | ``DATA_DIR``, ``ALPACA_PAPER_API_KEY``, ``BACKTEST_SLIPPAGE_PERCENT`` |
| Strategy | ``settings/strategies/store.json`` | ``INSTRUMENT``, ``FEATURE_*``, ``MODEL_*``, ``GATE_*``, ``RISK_*`` |

**Precedence: strategy > account > .env > schema default.** Both JSON files are
flat ``KEY -> raw value`` maps, so one loop overlays them onto the model:

    base = Settings()                  # .env + schema defaults
    eff  = Settings(**account, **strategy)

Because the gates and schedule now live in the strategy file,
a backtest and the live loop automatically evaluate each strategy with ITS own
these values — nothing else has to thread a strategy through.

The process-level cache is keyed on the mtimes of (.env, account file, strategy
file), so editing any layer invalidates it implicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from pydantic import ValidationError

from src.config.settings import Settings, get_settings
from src.config import account as account_mod
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
    """Return ``Settings`` = .env overlaid with the account file, then the strategy.

    Applied in that order, so a strategy value wins over an account value, which
    wins over ``.env``.
    """
    base = get_settings()
    layers = (
        ("account", account_mod.account_values(base)),
        ("strategy", strategy_config_for(strategy_name)),
    )
    if not any(overlay for _, overlay in layers):
        return Settings()  # no overrides anywhere -> plain global settings

    field_map = _field_by_env_key()
    kwargs: Dict[str, str] = {}
    for source, overlay in layers:
        for key, raw in overlay.items():
            field = field_map.get(str(key).strip().upper())
            if field is None:
                logger.debug("Ignoring non-Settings key in %s settings: %s", source, key)
                continue
            kwargs[field] = str(raw).strip()

    if not kwargs:
        return Settings()
    # DATA_DIR is the single folder the account configures; the two subfolders are
    # derived from it. Set them explicitly so a stale HISTORICAL_DATA_DIR left in
    # .env cannot pin the dataset somewhere other than the account's folder.
    if kwargs.get("data_dir") and not {"historical_data_dir", "backtest_dir"} & set(kwargs):
        kwargs.update(account_mod.derived_dirs(kwargs["data_dir"]))
    if kwargs.get("instrument"):
        kwargs["instrument"] = str(kwargs["instrument"]).strip().upper()
    try:
        eff = Settings(**kwargs)  # merges on top of .env + schema defaults
        logger.info("Effective settings resolved (account + strategy layers)")
        return eff
    except ValidationError as exc:
        logger.warning("Stored settings invalid — falling back to global defaults: %s", exc)
        return Settings()


def _file_signature(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _strategy_file_path() -> Path:
    return rules_mod.rules_file_path(get_settings())


def _account_file_path() -> Path:
    return account_mod.account_file_path(get_settings())


def get_effective_settings() -> Settings:
    """Cached effective Settings; recomputed automatically when .env, the account
    file or the strategy file changes (mtime signature), or after ``invalidate()``."""
    global _cache, _cache_key, _env_sig

    env_path = _ENV_PATH
    current_env = _file_signature(env_path)
    if current_env != _env_sig:
        # .env changed (or first call): reload the cached global settings too.
        _env_sig = current_env
        get_settings.cache_clear()
        _cache = None

    account_sig = _file_signature(_account_file_path())
    store_sig = _file_signature(_strategy_file_path())
    key = (_env_sig, account_sig, store_sig)
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
