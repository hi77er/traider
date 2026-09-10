"""Read + update the bot's central ``.env`` configuration for the Web Portal.

The portal exposes the whole config as an editable form:

- ``GET  /api/v1/config`` -> schema of sections/fields (secrets masked)
- ``POST /api/v1/config`` -> merge submitted values into ``.env`` atomically

Secrets (keys ending in ``_PASSWORD`` / ``_API_KEY`` / ``_SECRET`` / ``_TOKEN``)
are never sent to the browser: a set secret is shown as a mask and an empty /
masked value on save keeps the existing secret untouched.

Changing ``INSTRUMENT`` is allowed: the UI confirms first, and on save the
settings cache is invalidated so the whole portal immediately points at the
new instrument (its own Parquet dataset file, or the download state if that
file does not exist yet).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.config.settings import Settings

logger = logging.getLogger(__name__)

# Sentinel for a set-but-hidden secret in the UI.
MASK = "********"

# Keys the Web Portal refuses to write. Kept as a set for clarity; the
# instrument is intentionally NOT here — switching it is allowed (the UI
# confirms first) and the settings cache is invalidated on save so the whole
# dashboard switches to the new instrument immediately.
_PROTECTED: set = set()

_SENSITIVE_SUFFIXES = ("_PASSWORD", "_API_KEY", "_SECRET", "_TOKEN")

# (prefixes / exact keys, section name). Order here groups the fields that are
# defined on ``Settings``; fields themselves render in model-definition order.
_SECTION_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (
        (
            "INSTRUMENT", "DECISION_INTERVAL_HOURS", "TRADING_START_HOUR", "TRADING_END_HOUR",
            "MARKET_TIMEZONE", "DECISION_TIME", "DATA_DELTA_PULL_TIME",
        ),
        "Trading",
    ),
    (("OPENBB_", "DATA_CACHE_ENABLED", "CACHE_DIR"), "Market Data"),
    (("HISTORICAL_", "BACKTEST_START_DATE", "BACKTEST_END_DATE", "TRAIN_TEST_SPLIT", "LIVE_LOOKBACK_DAYS"), "Data & Storage"),
    (("FEATURE_",), "Features"),
    (("FEATURES_",), "Feature Parameters"),
    (("MODEL_", "STRATEGY_"), "Model"),
    (
        (
            "RISK_", "MAX_LOSS_PERCENT", "MAX_CONSECUTIVE_LOSSES", "MAX_EXPOSURE_PERCENT",
            "POSITION_SIZING_MODE", "STOP_LOSS_PERCENT", "TAKE_PROFIT_PERCENT",
            "CIRCUIT_BREAKER_ENABLED", "APPLY_RISK_LAYER",
        ),
        "Risk Management",
    ),
    (("GATE_", "BACKTEST_SLIPPAGE_PERCENT", "BACKTEST_COMMISSION_PER_TRADE"), "Backtest Gates"),
    (("IBKR_", "PAPER_TRADING", "EXECUTION_"), "Execution (IBKR)"),
    (("SCHEDULER_",), "Scheduler"),
    (("WEB_PORTAL_",), "Web Portal"),
]

# Keys rendered as a dropdown instead of a free-text field. Options may be a
# plain string (value == label) or a dict {label, value} for human labels with
# an underlying code (e.g. "1 hour" -> "1h").
_OPTIONS: Dict[str, List[Any]] = {
    "MODEL_TYPE": ["logistic_regression", "rule_based"],
    "POSITION_SIZING_MODE": ["fixed_risk", "volatility_target"],
    "HISTORICAL_BAR_SIZE": [
        {"label": "1 hour", "value": "1h"},
        {"label": "2 hours", "value": "2h"},
        {"label": "4 hours", "value": "4h"},
        {"label": "8 hours", "value": "8h"},
        {"label": "12 hours", "value": "12h"},
        {"label": "1 day", "value": "1d"},
    ],
    "HISTORICAL_LOOKBACK_YEARS": [
        {"label": "1 year", "value": "1"},
        {"label": "2 years", "value": "2"},
        {"label": "3 years", "value": "3"},
        {"label": "4 years", "value": "4"},
        {"label": "5 years", "value": "5"},
    ],
}

# Friendlier labels for the Features Engineering on/off toggles.
_LABELS: Dict[str, str] = {
    "HISTORICAL_LOOKBACK_YEARS": "Historical Period",
    "HISTORICAL_BAR_SIZE": "Historical Bar Size",
    "FEATURE_SMA_ENABLED": "Simple Moving Average (SMA)",
    "FEATURE_RSI_ENABLED": "Relative Strength Index (RSI)",
    "FEATURE_ATR_ENABLED": "Average True Range (ATR)",
    "FEATURE_BOLLINGER_ENABLED": "Bollinger Bands (%B)",
    "FEATURE_MOMENTUM_ENABLED": "Momentum",
    "FEATURE_VOLATILITY_ENABLED": "Volatility",
    "FEATURE_VWAP_ENABLED": "Volume-Weighted Average Price (VWAP)",
    "FEATURE_VOLUME_ENABLED": "Volume (vs. rolling average)",
    "FEATURE_VOLUME_ABS_ENABLED": "Volume (absolute per candle)",
    "ALLOW_SHORT": "Allow short positions",
    "APPLY_RISK_LAYER": "Apply the risk layer in backtests",
    "RISK_LIMIT_PERCENT": "Risk per trade (% of account)",
    "POSITION_SIZING_MODE": "Position sizing mode",
    "STOP_LOSS_PERCENT": "Stop loss (%)",
    "TAKE_PROFIT_PERCENT": "Take profit (%)",
    "MAX_EXPOSURE_PERCENT": "Max exposure (% of account)",
    "MAX_LOSS_PERCENT": "Max daily loss before halt (%)",
    "MAX_CONSECUTIVE_LOSSES": "Max consecutive losses",
    "CIRCUIT_BREAKER_ENABLED": "Circuit breaker enabled",
}

# ── Numeric bounds surfaced to the UI ──────────────────────────────────────
# pydantic v2 stores ``ge``/``gt``/``le``/``lt`` in ``FieldInfo.metadata`` as
# small wrapper objects (``Ge``/``Gt``/``Le``/``Lt``), each carrying its value
# (e.g. ``Ge{ge: 0.0}``). These become HTML ``min``/``max`` attributes and the
# UI's pre-submit range check.
def _field_bounds(field) -> Dict[str, Any]:
    """Inclusive {min, max} of a Settings field from its pydantic constraints."""
    lo = hi = None
    for meta in getattr(field, "metadata", ()) or ():
        attrs = vars(meta) if hasattr(meta, "__dict__") else {}
        if lo is None:
            for k in ("ge", "gt"):
                if k in attrs:
                    lo = attrs[k]
                    break
        if hi is None:
            for k in ("le", "lt"):
                if k in attrs:
                    hi = attrs[k]
                    break
    out: Dict[str, Any] = {}
    if lo is not None:
        out["min"] = lo
    if hi is not None:
        out["max"] = hi
    return out


# Curated examples/clarifications shown as their own hint line under a field,
# for keys whose description is thin or missing. Keep them short.
_HINTS: Dict[str, str] = {
    "MODEL_RETRAIN_INTERVAL_DAYS": "Ignored by the rule-based model.",
    "BACKTEST_SLIPPAGE_PERCENT": "Order slippage as a % of price, charged on each fill (e.g. 0.05 = 0.05%).",
    "IBKR_API_URL": "Base URL of the IBKR Client Portal Gateway you connect to.",
    "IBKR_ACCOUNT_ID": "Your Interactive Brokers account id.",
    "IBKR_USERNAME": "IBKR account username (if your gateway needs one).",
    "IBKR_PASSWORD": "IBKR password — stored masked, never shown.",
    "PAPER_TRADING": "True runs against the simulated paper account; False sends REAL orders.",
    "EXECUTION_MAX_RETRIES": "Attempts to send/fetch an order before giving up.",
    "EXECUTION_RETRY_BASE_DELAY_SECONDS": "Delay before the first retry; later retries back off.",
    "EXECUTION_ORDER_TIMEOUT_SECONDS": "Seconds to wait for an order acknowledgement before retrying.",
    "SCHEDULER_ENABLED": "Master switch for the scheduled daily jobs.",
    "SCHEDULER_TIMEZONE": "IANA timezone, e.g. America/New_York.",
    "OPENBB_PROVIDER": "Provider for candles/quotes — yfinance (free) or polygon/fmp (API key).",
    "BACKTEST_START_DATE": "Optional window start, YYYY-MM-DD; empty = start of the dataset.",
    "BACKTEST_END_DATE": "Optional window end, YYYY-MM-DD; empty = end of the dataset.",
    "TRAIN_TEST_SPLIT": "0.8 = use 80% for training, 20% held out.",
    "MARKET_TIMEZONE": "IANA name of the exchange's timezone, e.g. America/New_York.",
}


def _fmt_num(v: Any) -> str:
    """Plain number formatting for hints (no trailing '.0' on whole floats)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _meta_line(key: str, info) -> Optional[str]:
    """One short 'value type / allowed range' line for numeric fields.

    Booleans intentionally get no line: an on/off switch needs no explanation
    that it is a toggle.
    """
    ann = info.annotation
    if ann is bool:
        return None
    if ann not in (int, float):
        return None
    kind = "Whole number" if ann is int else "Number"
    b = _field_bounds(info)
    parts = []
    if b.get("min") is not None:
        parts.append(f"min {_fmt_num(b['min'])}")
    if b.get("max") is not None:
        parts.append(f"max {_fmt_num(b['max'])}")
    return f"{kind} — allowed " + ", ".join(parts) if parts else f"{kind} (no fixed range)"


def _field_hints(key: str, info) -> List[str]:
    """Discrete guidance lines shown under a settings input: description first,
    then the value type/range, then a curated example (if any)."""
    lines: List[str] = []
    desc = (info.description or "").strip()
    if desc:
        lines.append(desc)
    meta = _meta_line(key, info)
    if meta:
        lines.append(meta)
    extra = _HINTS.get(key)
    if extra:
        lines.append(extra)
    return lines

# ── Per-strategy settings ──────────────────────────────────────────────────
# Settings that can legitimately differ between strategies, so they are edited
# in the strategy panel and stored inside each strategy's `config` object in
# the strategy JSON file — NOT in the global .env form. Order matters (display).
_STRATEGY_SCOPE: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "Instrument",
        (
            "INSTRUMENT",
            "HISTORICAL_LOOKBACK_YEARS",
            "HISTORICAL_BAR_SIZE",
        ),
    ),
    ("Model", ("MODEL_BUY_THRESHOLD", "MODEL_SELL_THRESHOLD")),
    (
        "Features",
        (
            "FEATURE_SMA_ENABLED", "FEATURE_RSI_ENABLED", "FEATURE_ATR_ENABLED",
            "FEATURE_BOLLINGER_ENABLED", "FEATURE_MOMENTUM_ENABLED",
            "FEATURE_VOLATILITY_ENABLED", "FEATURE_VWAP_ENABLED",
            "FEATURE_VOLUME_ENABLED", "FEATURE_VOLUME_ABS_ENABLED",
        ),
    ),
    (
        "Feature Parameters",
        (
            "FEATURES_SMA_PERIODS", "FEATURES_RSI_PERIOD", "FEATURES_ATR_PERIOD",
            "FEATURES_BOLLINGER_PERIOD", "FEATURES_BOLLINGER_STD",
            "FEATURES_MOMENTUM_PERIODS", "FEATURES_VOLATILITY_PERIOD",
            "FEATURES_VWAP_PERIOD", "FEATURES_VOLUME_PERIOD", "FEATURES_MIN_LOOKBACK",
        ),
    ),
    (
        "Risk Management",
        (
            # The two master switches lead the panel: whether the layer runs at
            # all, then whether the breaker is armed. Everything below only
            # matters when the layer is on.
            "APPLY_RISK_LAYER",
            "CIRCUIT_BREAKER_ENABLED",
            "ALLOW_SHORT",
            "RISK_LIMIT_PERCENT", "POSITION_SIZING_MODE", "STOP_LOSS_PERCENT",
            "TAKE_PROFIT_PERCENT", "MAX_EXPOSURE_PERCENT", "MAX_LOSS_PERCENT",
            "MAX_CONSECUTIVE_LOSSES",
        ),
    ),
]
STRATEGY_SCOPED_KEYS = frozenset(k for _, keys in _STRATEGY_SCOPE for k in keys)

# Keys that are NOT shown in the global .env form right now:
# - ``S3_*`` / ``AWS_*`` / ``DYNAMODB_*`` — sync/storage parts not implemented
#   yet (surface them again once those features land).
# - ``WEB_PORTAL_ENABLED`` — the dashboard is essential while it is the only
#   UI, so its on/off switch must not be reachable from the form.
_HIDDEN_FROM_GLOBAL_PREFIXES = ("S3_", "AWS_", "DYNAMODB_")
_HIDDEN_FROM_GLOBAL_KEYS = frozenset({
    "WEB_PORTAL_ENABLED",
    # Historical window is now a years/bar-size choice in the strategy panel,
    # not free-text dates in the global .env form.
    "HISTORICAL_START_DATE",
    "HISTORICAL_END_DATE",
})


def _hidden_from_global(key: str) -> bool:
    """True when a key must never appear in (or be written by) the global form."""
    return key in _HIDDEN_FROM_GLOBAL_KEYS or key.startswith(_HIDDEN_FROM_GLOBAL_PREFIXES)


def _field_by_env_key() -> Dict[str, str]:
    """Map ``ENV_KEY`` -> Settings field name (e.g. FEATURE_SMA_ENABLED -> feature_sma_enabled)."""
    return {_env_key(f): f for f in Settings.model_fields}


# The risk settings live in their own panel ("Risk Management"), so they are
# removed from the Strategy Configuration panel to avoid duplicate inputs.
RISK_GROUP_NAME = "Risk Management"


def split_strategy_groups(groups: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Split the per-strategy groups into ``(configuration, risk)``.

    Each key must appear in exactly ONE panel: the risk layer has its own
    collapsible card under the Rules panel."""
    risk = [g for g in groups if g.get("name") == RISK_GROUP_NAME]
    rest = [g for g in groups if g.get("name") != RISK_GROUP_NAME]
    return rest, risk


def strategy_config_groups(settings: Settings, overrides: Optional[Dict[str, str]] = None) -> List[dict]:
    """Schema of the per-strategy settings (grouped), used by the strategy panel.

    ``overrides`` may carry the strategy's stored values; any missing key falls
    back to the current global default (read from ``settings``).
    """
    field_by_key = _field_by_env_key()
    groups: List[dict] = []
    for name, keys in _STRATEGY_SCOPE:
        fields: List[dict] = []
        for key in keys:
            field_name = field_by_key[key]
            info = Settings.model_fields[field_name]
            default = _format_value(getattr(settings, field_name))
            value = default if overrides is None else overrides.get(key, default)
            fields.append(
                {
                    "key": key,
                    "label": _LABELS.get(key, _human_label(key)),
                    "type": _field_type(info.annotation),
                    "value": value,
                    "default_value": default,
                    "description": info.description or "",
                    "options": _OPTIONS.get(key),
                    "hints": _field_hints(key, info),
                }
            )
            if bounds := _field_bounds(info):
                fields[-1].update(bounds)
        groups.append({"name": name, "fields": fields})
    return groups


def strategy_config_defaults(settings: Settings) -> Dict[str, str]:
    """Current global values (KEY -> raw string) of every per-strategy setting."""
    field_by_key = _field_by_env_key()
    return {key: _format_value(getattr(settings, field_by_key[key])) for key in STRATEGY_SCOPED_KEYS}


def validate_strategy_config(values: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Validate a per-strategy ``config`` dict (env KEY -> raw string).

    Only keys in ``STRATEGY_SCOPED_KEYS`` are accepted; values are validated
    through the real ``Settings`` model so bounds/choices still apply.
    """
    field_by_key = _field_by_env_key()
    errors: List[str] = []
    mapped: Dict[str, str] = {}
    for key, raw in values.items():
        key = str(key).strip().upper()
        if key not in STRATEGY_SCOPED_KEYS or key not in field_by_key:
            errors.append(f"{key}: not a per-strategy setting")
            continue
        mapped[field_by_key[key]] = str(raw).strip()
    if not errors:
        try:
            Settings(**mapped)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(map(str, err["loc"]))
                errors.append(f"{loc.upper()}: {err['msg']}")
    return (not errors), errors


def env_file_path() -> Path:
    """Absolute path of the ``.env`` file (project root, same as Settings)."""
    return Path(__file__).resolve().parents[3] / ".env"


# ---------------------------------------------------------------------------
# schema (GET)
# ---------------------------------------------------------------------------
def get_config_schema() -> dict:
    """Return the editable config schema grouped into sections (secrets masked)."""
    path = env_file_path()
    env = _read_env(path)
    settings = Settings()  # fresh instance so file edits are reflected

    sections: Dict[str, List[dict]] = {}
    order: List[str] = []
    for field_name, info in Settings.model_fields.items():
        key = _env_key(field_name)
        if key in STRATEGY_SCOPED_KEYS or _hidden_from_global(key):
            continue  # per-strategy / not-yet-exposed settings are not in the global form
        sensitive = _is_sensitive(key)
        section = _section_for(key)
        if section not in sections:
            sections[section] = []
            order.append(section)

        set_in_file = key in env
        if sensitive:
            value = MASK if set_in_file and env[key].strip() else ""
        elif set_in_file:
            value = env[key].strip()
        else:
            value = _format_value(getattr(settings, field_name))

        field = {
            "key": key,
            "label": _LABELS.get(key, _human_label(key)),
            "type": _field_type(info.annotation),
            "value": value,
            "set": set_in_file,
            "sensitive": sensitive,
            "readonly": key in _PROTECTED,
            "description": info.description or "",
            "options": _OPTIONS.get(key),
            "hints": _field_hints(key, info),
        }
        field.update(_field_bounds(info))
        sections[section].append(field)

    return {
        "file": str(path),
        "file_exists": path.exists(),
        "sections": [{"name": name, "fields": sections[name]} for name in order],
    }


# ---------------------------------------------------------------------------
# update (POST)
# ---------------------------------------------------------------------------
def update_config(values: Dict[str, str]) -> dict:
    """Merge submitted values into ``.env`` (atomic), revalidating first.

    Sensitive keys submitted empty / masked are left unchanged so existing
    secrets are never clobbered by an unedited form field.
    """
    path = env_file_path()
    env = _read_env(path) if path.exists() else {}
    field_by_key = {_env_key(f): f for f in Settings.model_fields}

    # 1) Validate the merged config against the Pydantic model.
    overrides: Dict[str, str] = {}
    for key, submitted in values.items():
        if key not in field_by_key:
            continue  # unknown keys are ignored
        if key in _PROTECTED:
            continue  # protected keys are never changed via the API
        if key in STRATEGY_SCOPED_KEYS:
            continue  # per-strategy keys are managed in the strategy panel only
        if _hidden_from_global(key):
            continue  # not editable through the global form (yet)
        if _is_sensitive(key) and submitted in ("", MASK):
            continue  # keep existing secret
        overrides[key] = submitted.strip()
    for key, raw in env.items():  # unchanged file values also validated
        if key in field_by_key and key not in overrides:
            overrides[key] = raw.strip()

    try:
        Settings(**{field_by_key[k]: v for k, v in overrides.items()})
    except ValidationError as exc:
        errors = [f"{err['loc'][0].upper()}: {err['msg']}" for err in exc.errors()]
        logger.warning("Config update rejected: %s", errors)
        return {"ok": False, "message": "Invalid configuration", "errors": errors, "file": str(path)}

    # 2) Merge into the file, preserving comments / ordering / inline comments.
    if path.exists():
        lines, key_to_line = _parse_env_file(path.read_text(encoding="utf-8"))
    else:
        lines = ["# TRAIDER configuration — generated from the Web Portal", ""]
        key_to_line = {}

    changed: List[str] = []
    for key, submitted in values.items():
        if key not in field_by_key:
            continue
        if key in _PROTECTED:
            continue
        if key in STRATEGY_SCOPED_KEYS:
            continue  # never written to .env — per strategy only
        if _hidden_from_global(key):
            continue  # not editable through the global form (yet)
        if _is_sensitive(key) and submitted in ("", MASK):
            continue
        value = submitted.strip()
        if key in key_to_line:
            lines[key_to_line[key]] = _replace_value(lines[key_to_line[key]], key, value)
        else:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"{key}={value}")
        changed.append(key)

    _atomic_write(path, "\n".join(lines) + "\n")
    logger.info("Web Portal updated .env keys: %s", ", ".join(changed) or "(none)")

    return {
        "ok": True,
        "message": f"Saved {len(changed)} setting(s) to {path.name}",
        "errors": [],
        "file": str(path),
        "updated": changed,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read_env(path: Path) -> Dict[str, str]:
    """Parse a dotenv file into an ordered ``KEY -> raw value`` mapping."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        if key:
            values[key] = val.strip()
    return values


def _parse_env_file(text: str) -> Tuple[List[str], Dict[str, int]]:
    """Split file into lines plus a ``KEY -> line index`` map (comments kept)."""
    lines: List[str] = []
    key_to_line: Dict[str, int] = {}
    for i, line in enumerate(text.splitlines()):
        lines.append(line)
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key:
                key_to_line[key] = i
    return lines, key_to_line


def _replace_value(line: str, key: str, value: str) -> str:
    """Rewrite ``KEY=value`` in place, preserving a trailing inline comment."""
    _, _, rest = line.partition("=")
    comment = ""
    val_part = rest
    if "#" in val_part:
        val_part, _, comment = val_part.partition("#")
        comment = comment.rstrip()
    new = f"{key}={value}"
    if comment:
        new += "  # " + comment
    return new


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + rename so a crash never leaves a half-written .env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
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


def _env_key(field_name: str) -> str:
    return field_name.upper()


def _field_type(annotation: Any) -> str:
    from typing import get_args

    inner = annotation
    args = get_args(annotation)
    if args:
        non_none = [a for a in args if a is not type(None)]
        inner = non_none[0] if non_none else annotation
    if inner is bool:
        return "bool"
    if inner is int:
        return "int"
    if inner is float:
        return "float"
    return "str"


def _is_sensitive(key: str) -> bool:
    return any(key.endswith(s) for s in _SENSITIVE_SUFFIXES)


def _section_for(key: str) -> str:
    for prefixes, name in _SECTION_RULES:
        if any(key == p or key.startswith(p) for p in prefixes):
            return name
    return "Other"


def _human_label(key: str) -> str:
    return key.replace("_", " ").title()


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)
