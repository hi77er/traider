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

from src.config import account as account_mod
from src.config.effective import invalidate
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
#
# Only MACHINE-level settings stay global. Everything else moved to one of the
# two JSON layers: trading/storage/broker defaults live in Account Settings
# (``settings/account/account.json``) and anything a strategy needs lives in the
# strategy store (``settings/strategies/store.json``).
_SECTION_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("OPENBB_",), "Market Data"),
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

# The paper/live switch, served to the header dropdown so the client reads it from
# the server instead of hard-coding a list. Deliberately NOT in _OPTIONS: no
# settings panel renders it any more — the environment is chosen from the header,
# and the trading on/off switch lives in the Execution panel.
EXECUTION_ENV_OPTIONS: List[Dict[str, str]] = [
    {"label": "Paper — simulated, no real money", "value": "paper"},
    {"label": "LIVE — REAL ORDERS", "value": "live"},
]

# Friendlier labels for the Features Engineering on/off toggles.
_LABELS: Dict[str, str] = {
    "HISTORICAL_LOOKBACK_YEARS": "Historical Period",
    "HISTORICAL_BAR_SIZE": "Historical Bar Size",
    "FEATURE_SMA_ENABLED": "Simple Moving Average (SMA)",
    "FEATURE_EMA_ENABLED": "Exponential Moving Average (EMA)",
    "FEATURE_MACD_ENABLED": "MACD (moving average convergence/divergence)",
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
    # ── Account settings (settings/account/account.json) ──────────
    "DATA_DIR": "Data folder",
    "HISTORICAL_DATA_DIR": "Historical data subfolder",
    "BACKTEST_DIR": "Backtest data subfolder",
    "ALPACA_PAPER_API_KEY": "Alpaca paper API key",
    "ALPACA_PAPER_API_SECRET": "Alpaca paper API secret",
    "ALPACA_LIVE_API_KEY": "Alpaca live API key",
    "ALPACA_LIVE_API_SECRET": "Alpaca live API secret",
    "EXECUTION_ENV": "Order environment (paper / live)",
    "BACKTEST_START_DATE": "Backtest window start",
    "BACKTEST_END_DATE": "Backtest window end",
    "TRAIN_TEST_SPLIT": "Train/test split",
    "BACKTEST_SLIPPAGE_PERCENT": "Slippage per fill (%)",
    "BACKTEST_COMMISSION_PER_TRADE": "Commission per trade (USD)",
    "DATA_CACHE_ENABLED": "Cache fetched candles",
    "CACHE_DIR": "Cache folder",
    "HISTORICAL_START_DATE": "History fetch start",
    "HISTORICAL_END_DATE": "History fetch end",
    "LIVE_LOOKBACK_DAYS": "Live poll lookback (days)",
    "S3_ENABLED": "Sync dataset to S3",
    "S3_BUCKET": "S3 bucket",
    "S3_PREFIX": "S3 key prefix",
    "S3_ENDPOINT_URL": "S3 endpoint (MinIO)",
    "AWS_REGION": "AWS region",
    "DYNAMODB_TABLE": "State table",
    "DYNAMODB_TTL_DAYS": "State TTL (days)",
    "DYNAMODB_ENDPOINT_URL": "DynamoDB endpoint (local)",
    # ── Strategy settings that moved out of the global form ───────
    "DECISION_INTERVAL_HOURS": "Decision interval (hours)",
    "TRADING_START_HOUR": "Trading window start",
    "TRADING_END_HOUR": "Trading window end",
    "MARKET_TIMEZONE": "Market timezone",
    "DECISION_TIME": "Daily decision time",
    "DATA_DELTA_PULL_TIME": "Daily delta pull time",
    "MODEL_TYPE": "Model type",
    "GATE_MIN_SHARPE": "Gate: min Sharpe",
    "GATE_MAX_DRAWDOWN_PERCENT": "Gate: max drawdown (%)",
    "GATE_MIN_WIN_RATE_PERCENT": "Gate: min win rate (%)",
    "GATE_MAX_WEEKLY_LOSS_PERCENT": "Gate: max weekly loss (%)",
    "SCHEDULER_ENABLED": "Scheduler enabled",
    "SCHEDULER_TIMEZONE": "Scheduler timezone",
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
    "FEATURES_EMA_PERIODS": "An EMA weights recent bars more than an SMA, so it turns faster "
    "(9 = fast, 21 = medium, 50 = slow).",
    "BACKTEST_SLIPPAGE_PERCENT": "Order slippage as a % of price, charged on each fill (e.g. 0.05 = 0.05%).",
    "EXECUTION_ENV": "paper = the simulated account (safe). live = REAL orders, "
    "which also needs a confirmation every time trading is turned on.",
    "ALPACA_PAPER_API_KEY": "From the Alpaca dashboard with the Paper account "
    "selected. Paper and live keys are DIFFERENT — never mix them.",
    "ALPACA_PAPER_API_SECRET": "Shown once when generated. Paper key secret.",
    "ALPACA_LIVE_API_KEY": "Only needed when EXECUTION_ENV is live.",
    "ALPACA_LIVE_API_SECRET": "Only needed when EXECUTION_ENV is live.",
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
    # NOTE: there is deliberately no "Execution" group here. The paper/live
    # environment is still a per-strategy setting, but it is edited from the
    # header dropdown, and the trading on/off switch lives in its own Execution
    # panel. See PANEL_HIDDEN_STRATEGY_KEYS below.
    (
        "Instrument",
        (
            "INSTRUMENT",
            "HISTORICAL_LOOKBACK_YEARS",
            "HISTORICAL_BAR_SIZE",
        ),
    ),
    (
        "Trading",
        (
            # The trading window + decision cadence belong to the strategy: two
            # strategies on the same instrument can trade different sessions.
            "DECISION_INTERVAL_HOURS", "MARKET_TIMEZONE", "TRADING_START_HOUR",
            "TRADING_END_HOUR", "DECISION_TIME", "DATA_DELTA_PULL_TIME",
        ),
    ),
    (
        "Model",
        (
            "MODEL_TYPE", "MODEL_BUY_THRESHOLD", "MODEL_SELL_THRESHOLD",
            "MODEL_RETRAIN_INTERVAL_DAYS",
        ),
    ),
    (
        "Features",
        (
            "FEATURE_SMA_ENABLED", "FEATURE_EMA_ENABLED", "FEATURE_MACD_ENABLED",
            "FEATURE_RSI_ENABLED", "FEATURE_ATR_ENABLED",
            "FEATURE_BOLLINGER_ENABLED", "FEATURE_MOMENTUM_ENABLED",
            "FEATURE_VOLATILITY_ENABLED", "FEATURE_VWAP_ENABLED",
            "FEATURE_VOLUME_ENABLED", "FEATURE_VOLUME_ABS_ENABLED",
        ),
    ),
    (
        "Feature Parameters",
        (
            "FEATURES_SMA_PERIODS", "FEATURES_EMA_PERIODS",
            "FEATURES_MACD_FAST_PERIOD", "FEATURES_MACD_SLOW_PERIOD",
            "FEATURES_MACD_SIGNAL_PERIOD",
            "FEATURES_RSI_PERIOD", "FEATURES_ATR_PERIOD",
            "FEATURES_BOLLINGER_PERIOD", "FEATURES_BOLLINGER_STD",
            "FEATURES_MOMENTUM_PERIODS", "FEATURES_VOLATILITY_PERIOD",
            "FEATURES_VWAP_PERIOD", "FEATURES_VOLUME_PERIOD", "FEATURES_MIN_LOOKBACK",
        ),
    ),
    (
        "Backtest Gates",
        (
            # Thresholds a backtest must clear — evaluated per strategy, so a
            # scalping strategy can hold itself to a different Sharpe bar than
            # a swing strategy.
            "GATE_MIN_SHARPE", "GATE_MAX_DRAWDOWN_PERCENT",
            "GATE_MIN_WIN_RATE_PERCENT", "GATE_MAX_WEEKLY_LOSS_PERCENT",
        ),
    ),
    ("Scheduler", ("SCHEDULER_ENABLED", "SCHEDULER_TIMEZONE")),
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
# Keys that ARE per-strategy and valid to store, but are NOT rendered by the
# strategy panel because they have their own home in the UI — EXECUTION_ENV is
# edited from the header dropdown. rules_service preserves them across a save,
# otherwise saving the panel would silently reset a live strategy back to paper.
PANEL_HIDDEN_STRATEGY_KEYS = frozenset({"EXECUTION_ENV"})

# Keys that used to be per-strategy and are now gone. A stored strategy may still
# carry them, so a save must DROP them rather than reject the whole config —
# otherwise a strategy saved before the removal could never be saved again.
RETIRED_STRATEGY_KEYS = frozenset({"EXECUTION_LIVE_ACK"})

STRATEGY_SCOPED_KEYS = (
    frozenset(k for _, keys in _STRATEGY_SCOPE for k in keys) | PANEL_HIDDEN_STRATEGY_KEYS
)

# ── Account settings (settings/account/account.json) ──────────────────────
# What is true of this trading ACCOUNT: the broker it trades through, where its
# data and backtest runs are stored, and the backtest defaults. One file shared
# by every strategy; precedence is strategy > account > .env.
_ACCOUNT_SECTIONS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "Trading Account",
        (
            # The Alpaca credentials describe this ACCOUNT, so they are shared by
            # every strategy. Which ENVIRONMENT an order is sent to is per-strategy
            # — see the "Execution" group in _STRATEGY_SCOPE.
            "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
            "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET",
            "EXECUTION_MAX_RETRIES", "EXECUTION_RETRY_BASE_DELAY_SECONDS",
            "EXECUTION_ORDER_TIMEOUT_SECONDS",
        ),
    ),
    (
        "Data & Folders",
        (
            # DATA_DIR is the single folder the user picks; the two subfolders
            # below are DERIVED from it (read-only in the popup).
            "DATA_DIR", "HISTORICAL_DATA_DIR", "BACKTEST_DIR",
            "DATA_CACHE_ENABLED", "CACHE_DIR",
            "HISTORICAL_START_DATE", "HISTORICAL_END_DATE", "LIVE_LOOKBACK_DAYS",
        ),
    ),
    (
        "Backtest",
        (
            "BACKTEST_START_DATE", "BACKTEST_END_DATE", "TRAIN_TEST_SPLIT",
            "BACKTEST_SLIPPAGE_PERCENT", "BACKTEST_COMMISSION_PER_TRADE",
        ),
    ),
    ("Cloud Storage", ("S3_ENABLED", "S3_BUCKET", "S3_PREFIX", "S3_ENDPOINT_URL")),
    (
        "State Storage",
        ("AWS_REGION", "DYNAMODB_TABLE", "DYNAMODB_TTL_DAYS", "DYNAMODB_ENDPOINT_URL"),
    ),
]
ACCOUNT_SCOPED_KEYS = frozenset(k for _, keys in _ACCOUNT_SECTIONS for k in keys)

# Derived from DATA_DIR — shown so the layout is visible, never stored.
_DERIVED_ACCOUNT_KEYS = frozenset({"HISTORICAL_DATA_DIR", "BACKTEST_DIR"})

# Keys that are NOT shown in the global .env form:
# - ``WEB_PORTAL_*`` — the portal is being reworked; its switches are parked.
# - internal file paths — implied by the folder layout, not user-editable.
# - ``S3_*`` / ``AWS_*`` / ``DYNAMODB_*`` — now edited in Account Settings.
_HIDDEN_FROM_GLOBAL_PREFIXES = ("WEB_PORTAL_",)
_HIDDEN_FROM_GLOBAL_KEYS = frozenset({
    # Paths to the two JSON stores (see ACCOUNT_SETTINGS_FILE / STRATEGY_RULES_FILE).
    "STRATEGY_RULES_FILE",
    "ACCOUNT_SETTINGS_FILE",
    # The dashboard is essential while it is the only UI, so its on/off switch
    # must not be reachable from the form.
    "WEB_PORTAL_ENABLED",
    # Historical window is a years/bar-size choice in the strategy panel, and a
    # fetch-window default in Account Settings — not free-text in the global form.
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
        if key in STRATEGY_SCOPED_KEYS or key in ACCOUNT_SCOPED_KEYS or _hidden_from_global(key):
            continue  # per-strategy / per-account / parked settings are not in the global form
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
        if key in ACCOUNT_SCOPED_KEYS:
            continue  # account keys are managed in the Account Settings popup only
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
        if key in ACCOUNT_SCOPED_KEYS:
            continue  # never written to .env — per account only
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


# ---------------------------------------------------------------------------
# Account settings (settings/account/account.json)
# ---------------------------------------------------------------------------
def account_sections(settings: Optional[Settings] = None) -> List[dict]:
    """Sections/fields of the account settings, with the value in force for each.

    Mirrors ``strategy_config_groups``: a stored account value wins, otherwise
    the current ``.env`` / schema default is shown — so the popup never displays
    a value the bot is not actually using.
    """
    settings = settings or Settings()
    stored = account_mod.account_values(settings)
    field_by_key = _field_by_env_key()
    # Show values as the bot would USE them: the account layer applied on top of
    # the .env defaults, so the derived folders reflect the account's own DATA_DIR.
    try:
        shown_kwargs: Dict[str, str] = {
            field_by_key[k]: v for k, v in stored.items()
            if k in ACCOUNT_SCOPED_KEYS and k in field_by_key
        }
        if shown_kwargs.get("data_dir") and not {"historical_data_dir", "backtest_dir"} & set(shown_kwargs):
            shown_kwargs.update(account_mod.derived_dirs(shown_kwargs["data_dir"]))
        shown = Settings(**shown_kwargs)
    except ValidationError:
        shown = settings
    groups: List[dict] = []
    for name, keys in _ACCOUNT_SECTIONS:
        fields: List[dict] = []
        for key in keys:
            field_name = field_by_key[key]
            info = Settings.model_fields[field_name]
            default_value = _format_value(getattr(shown, field_name))
            raw = stored.get(key)
            sensitive = _is_sensitive(key)
            field = {
                "key": key,
                "label": _LABELS.get(key, _human_label(key)),
                "type": _field_type(info.annotation),
                # A set secret is never sent to the browser.
                "value": (MASK if sensitive and raw else (raw if raw is not None else default_value)),
                "default_value": default_value,
                "set": raw is not None,
                "sensitive": sensitive,
                "readonly": key in _DERIVED_ACCOUNT_KEYS,
                "readonly_note": (
                    "Derived from the data folder above" if key in _DERIVED_ACCOUNT_KEYS else None
                ),
                "description": info.description or "",
                "options": _OPTIONS.get(key),
                "hints": _field_hints(key, info),
            }
            field.update(_field_bounds(info))
            fields.append(field)
        groups.append({"name": name, "fields": fields})
    return groups


def get_account_schema() -> dict:
    """Payload for the Account Settings popup."""
    settings = Settings()
    path = account_mod.account_file_path(settings)
    return {
        "file": str(path),
        "file_exists": path.exists(),
        "groups": account_sections(settings),
        "error": account_mod.account_error(settings),
    }


def update_account(values: Dict[str, str]) -> dict:
    """Validate + persist the account settings into ``account.json``.

    Secret fields submitted empty / masked keep their stored value, exactly like
    the global form, so an unedited password field can never wipe a credential.
    """
    settings = Settings()
    stored = {k: v for k, v in account_mod.account_values(settings).items() if k in ACCOUNT_SCOPED_KEYS}
    field_by_key = _field_by_env_key()

    merged: Dict[str, str] = dict(stored)
    errors: List[str] = []
    for key, submitted in values.items():
        env_key = str(key).strip().upper()
        if env_key not in ACCOUNT_SCOPED_KEYS or env_key not in field_by_key:
            errors.append(f"{env_key}: not an account setting")
            continue
        if env_key in _DERIVED_ACCOUNT_KEYS:
            continue  # derived from DATA_DIR, never stored
        if _is_sensitive(env_key) and submitted in ("", MASK):
            continue  # keep the stored secret
        merged[env_key] = str(submitted).strip()

    if not errors:
        try:
            Settings(**{field_by_key[k]: v for k, v in merged.items()})
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(map(str, err["loc"]))
                errors.append(f"{loc.upper()}: {err['msg']}")
    if errors:
        logger.warning("Account settings rejected: %s", errors)
        return {"ok": False, "message": "Invalid account settings", "errors": errors}

    path = account_mod.save_account(settings, merged)
    invalidate()  # the account layer feeds resolve_effective()
    logger.info("Account settings saved to %s", path)
    return {
        "ok": True,
        "message": f"Saved {len(merged)} setting(s) to {path.name}",
        "errors": [],
        "file": str(path),
        "groups": account_sections(Settings()),
    }


def _human_label(key: str) -> str:
    return key.replace("_", " ").title()


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)
