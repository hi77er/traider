"""Account-level settings store — one JSON file shared by EVERY strategy.

TRAIDER's configuration has three layers, and this module owns the middle one:

| Layer | Lives in | Answers |
|-------|----------|---------|
| **Global** | ``.env`` | how this *machine* reaches the outside world (data provider + keys) |
| **Account** | ``settings/account/account.json`` (this module) | what is true of *this trading account* — broker (Trading Account), backtest defaults, where data and results are stored |
| **Strategy** | ``settings/strategies/store.json`` | how *this strategy* trades — instrument, bar size, features, model, gates, risk, schedule |

The account document is a flat ``KEY -> value`` map so the SAME overlay
machinery as a strategy's ``config`` can merge it (see ``effective.py``):
values are env-key spellings (``IBKR_ACCOUNT_ID``, ``DATA_DIR``, …) and are
validated by the ``Settings`` model, so there is exactly one definition of every
setting.

Precedence at runtime: **strategy > account > .env > schema default**.

The file is local state: it is gitignored, may hold broker credentials, and an
absent file simply means "no account overrides yet" (all defaults apply).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config.settings import Settings

logger = logging.getLogger(__name__)

# Default location; override with ACCOUNT_SETTINGS_FILE in .env.
DEFAULT_ACCOUNT_FILE = "settings/account/account.json"
ACCOUNT_VERSION = 1


class AccountStore(BaseModel):
    """The account settings document written to ``account.json``."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(default=ACCOUNT_VERSION, ge=1)
    updated_at: Optional[str] = Field(default=None, description="ISO timestamp of the last save")
    settings: Dict[str, str] = Field(
        default_factory=dict,
        description="Account settings, env KEY -> raw value (e.g. DATA_DIR, IBKR_ACCOUNT_ID)",
    )


def account_file_path(settings: Settings) -> Path:
    """Path of the account file (relative paths resolve against the project root)."""
    return Path(settings.account_settings_file or DEFAULT_ACCOUNT_FILE)


def derived_dirs(data_dir: str) -> Dict[str, str]:
    """The two subfolders of the account's single data folder.

    DATA_DIR is the ONE folder the user picks; the dataset and the backtest runs
    are fixed subfolders of it, so they can never drift apart. Returned as
    ``Settings`` field names so callers can merge them straight into an overlay.
    """
    root = str(data_dir or "data").strip().rstrip("/") or "data"
    return {
        "historical_data_dir": f"{root}/historical",
        "backtest_dir": f"{root}/backtest_results",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".account.", suffix=".tmp")
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


def load_account(settings: Settings) -> AccountStore:
    """Load the account file; an absent or corrupt file yields an EMPTY store.

    A corrupt file is never overwritten here — the next explicit save does that —
    so a broken document can still be inspected/repaired by hand.
    """
    path = account_file_path(settings)
    if not path.exists():
        return AccountStore()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Account file %s is unreadable — using defaults: %s", path, exc)
        return AccountStore()
    try:
        if isinstance(raw, dict) and "settings" in raw:
            return AccountStore.model_validate(raw)
        if isinstance(raw, dict):
            # Flat {KEY: value} file (hand-written) — accept it as the settings map.
            return AccountStore(settings={str(k): str(v) for k, v in raw.items() if k != "version"})
        return AccountStore()
    except ValidationError as exc:
        logger.warning("Account file %s is invalid — using defaults: %s", path, exc)
        return AccountStore()


def account_values(settings: Settings) -> Dict[str, str]:
    """The stored account overrides (``{}`` when the file is absent)."""
    return dict(load_account(settings).settings or {})


def save_account(settings: Settings, values: Dict[str, str]) -> Path:
    """Validate + atomically persist the account settings.

    Only keys that map to a real ``Settings`` field are stored; unknown keys are
    dropped with a warning so a typo cannot silently accumulate in the file.
    """
    field_by_env_key = {name.upper(): name for name in Settings.model_fields}
    clean: Dict[str, str] = {}
    for key, value in (values or {}).items():
        env_key = str(key).strip().upper()
        if not env_key:
            continue
        if env_key not in field_by_env_key:
            logger.warning("Ignoring unknown account setting: %s", env_key)
            continue
        clean[env_key] = "" if value is None else str(value).strip()

    store = AccountStore(version=ACCOUNT_VERSION, updated_at=_now(), settings=clean)
    path = account_file_path(settings)
    content = json.dumps(store.model_dump(exclude_none=False), indent=2, default=str) + "\n"
    _atomic_write(path, content)
    logger.info("Saved %d account setting(s) to %s", len(clean), path)
    return path


def account_error(settings: Settings) -> Optional[str]:
    """Human message when the account file exists but cannot be parsed."""
    path = account_file_path(settings)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "settings" in raw:
            AccountStore.model_validate(raw)
        return None
    except Exception as exc:  # noqa: BLE001 - any parse/validation failure
        return f"Account file could not be parsed: {exc}"
