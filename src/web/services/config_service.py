"""Schema + persistence for the settings the Web Portal edits.

Two JSON layers, one popup each:

- ``settings/account/account.json`` — what is true of this trading ACCOUNT: the
  broker it trades through, where its data and backtest results live, the backtest
  defaults. Shared by every strategy (``GET/POST /api/v1/account``).
- ``settings/strategies/store.json`` — anything a STRATEGY needs (see
  ``rules_service``).

Global, infrastructure-level settings are no longer editable in the portal: they are
edited in ``.env`` directly, and ``Settings`` loads them from there. Nothing here
writes ``.env`` — it is the operator's file again.

Secrets (keys ending in ``_PASSWORD`` / ``_API_KEY`` / ``_SECRET`` / ``_TOKEN``) are
never sent to the browser: a set secret is shown as a mask, and an empty or masked
field on save keeps the stored value untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.config import account as account_mod
from src.config import history
from src.config.effective import get_effective_settings, invalidate
from src.config.settings import Settings
from src.execution import credentials as credentials_mod

logger = logging.getLogger(__name__)

# Sentinel for a set-but-hidden secret in the UI.
MASK = "********"

_SENSITIVE_SUFFIXES = ("_PASSWORD", "_API_KEY", "_SECRET", "_TOKEN")

# Keys rendered as a dropdown instead of a free-text field. Options may be a
# plain string (value == label) or a dict {label, value} for human labels with
# an underlying code (e.g. "1 hour" -> "1h").
_OPTIONS: Dict[str, List[Any]] = {
    "MODEL_TYPE": ["logistic_regression", "rule_based"],
    "POSITION_SIZING_MODE": ["fixed_risk", "volatility_target"],
    # HISTORICAL_BAR_SIZE and HISTORICAL_LOOKBACK are NOT here: the period a bar
    # size may be fetched for depends on the bar size, so both lists are built
    # from src/config/history.py in ``strategy_config_groups``.
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
    "HISTORICAL_BAR_SIZE": "Historical Bar Size",
    "HISTORICAL_LOOKBACK": "Historical Period",
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
    "BACKTEST_SLIPPAGE_PERCENT": "Slippage per fill (%)",
    "BACKTEST_COMMISSION_PER_TRADE": "Commission per trade (USD)",
    "DATA_CACHE_ENABLED": "Cache fetched candles",
    "CACHE_DIR": "Cache folder",
    "LIVE_LOOKBACK_DAYS": "Live poll lookback (days)",
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
            # The bar size comes FIRST: the period under it is limited to what
            # this bar size can actually be fetched for, so the pair reads in the
            # order the decision is made (pick the candle, then how far back).
            "INSTRUMENT",
            "HISTORICAL_BAR_SIZE",
            "HISTORICAL_LOOKBACK",
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
            "LIVE_LOOKBACK_DAYS",
        ),
    ),
    (
        "Backtest",
        (
            # No window and no split: a backtest is triggered by hand and covers the
            # whole period the STRATEGY is configured for, and the only model in use
            # is rule-based, so there is nothing to train on. A window here would be a
            # second, quieter answer to a question the strategy panel already answers.
            "BACKTEST_SLIPPAGE_PERCENT", "BACKTEST_COMMISSION_PER_TRADE",
        ),
    ),
]
# NOT here, and deliberately: cloud storage (S3_*) and state persistence
# (AWS_REGION / DYNAMODB_*). Both are infrastructure — one dataset copy per bucket,
# one state table per deployment — rather than properties of a trading account, and
# neither is being developed yet. They stay on ``Settings`` and are configured in
# `.env` when they are, which is also where their values already live.
ACCOUNT_SCOPED_KEYS = frozenset(k for _, keys in _ACCOUNT_SECTIONS for k in keys)

# Derived from DATA_DIR — shown so the layout is visible, never stored.
_DERIVED_ACCOUNT_KEYS = frozenset({"HISTORICAL_DATA_DIR", "BACKTEST_DIR"})


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
    # The bar size the panel will show, so the period list it offers is the one
    # that goes with THAT bar size (a caller may pass the strategy's stored values
    # as overrides without the resolver having been involved).
    bar_size = str((overrides or {}).get("HISTORICAL_BAR_SIZE") or "").strip() or getattr(
        settings, "historical_bar_size", ""
    )
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
            if key == "HISTORICAL_BAR_SIZE":
                fields[-1]["options"] = [
                    {"label": label, "value": code} for code, label in history.BAR_SIZES
                ]
            elif key == "HISTORICAL_LOOKBACK":
                # The periods on offer are the ones THIS bar size may be fetched
                # for, and the table travels with the field so the browser can
                # repopulate the list the moment the bar size changes — the panel
                # can then never show (or submit) a combination the rule forbids.
                fields[-1]["options"] = history.periods_for_options(bar_size)
                fields[-1]["options_by"] = history.periods_by_bar_size()
                fields[-1]["depends_on"] = "HISTORICAL_BAR_SIZE"
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


# A key pair can only be proved to WORK by asking Alpaca (see
# ``src/execution/credentials.py``), so the popup gets a Validate button and a
# verdict next to each pair. Attached to the SECRET — the last field of the pair — so
# the control lands under the pair it checks, and the spec carries BOTH field keys so
# neither the client nor this module has to guess which two inputs make a pair.
_VERIFY_PAIRS: Dict[str, Tuple[str, str]] = {
    "paper": ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET"),
    "live": ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET"),
}


def _verify_spec(key: str) -> Optional[dict]:
    """The credential pair this field closes, if it closes one."""
    for env, (key_key, secret_key) in _VERIFY_PAIRS.items():
        if key == secret_key:
            return {"env": env, "key_key": key_key, "secret_key": secret_key}
    return None


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
    # Show values as the bot would USE them: the caller's settings with the account
    # file on top, so the derived folders reflect the account's own DATA_DIR.
    #
    # The overlay is a copy of THOSE settings, not a fresh ``Settings()``: building a
    # new one dropped whatever the caller had passed in, so a credential that arrived
    # any way other than .env or the account file (the two a bare Settings can see)
    # was reported as unset — the popup said "nothing is configured" about a pair it
    # had just been handed.
    shown_kwargs: Dict[str, str] = {
        field_by_key[k]: v for k, v in stored.items()
        if k in ACCOUNT_SCOPED_KEYS and k in field_by_key
    }
    if shown_kwargs.get("data_dir") and not {"historical_data_dir", "backtest_dir"} & set(shown_kwargs):
        shown_kwargs.update(account_mod.derived_dirs(shown_kwargs["data_dir"]))
    shown = settings.model_copy(update=shown_kwargs)
    groups: List[dict] = []
    for name, keys in _ACCOUNT_SECTIONS:
        fields: List[dict] = []
        for key in keys:
            field_name = field_by_key[key]
            info = Settings.model_fields[field_name]
            shown_value = _format_value(getattr(shown, field_name))
            raw = stored.get(key)
            sensitive = _is_sensitive(key)
            effective = _format_value(raw) if raw is not None else shown_value
            field = {
                "key": key,
                "label": _LABELS.get(key, _human_label(key)),
                "type": _field_type(info.annotation),
                # A secret is never sent to the browser — in ANY field of the payload.
                # `default_value` is the value in force, so it leaks exactly as badly as
                # `value`, and a secret can arrive from `.env` as well as from the account
                # file; masking only the "stored" case left both of those open.
                "value": _mask_secret(effective, sensitive),
                "default_value": _mask_secret(shown_value, sensitive),
                # "A secret is in force" — not merely "this key appears in the account
                # file". A pair can arrive from .env as well, and calling that unset made
                # the form say "(unset)" for a credential the bot was really using. The
                # flag is a boolean, so it can never carry the secret itself.
                "set": bool(effective),
                "sensitive": sensitive,
                "readonly": key in _DERIVED_ACCOUNT_KEYS,
                "readonly_note": (
                    "Derived from the data folder above" if key in _DERIVED_ACCOUNT_KEYS else None
                ),
                "description": info.description or "",
                "options": _OPTIONS.get(key),
                "hints": _field_hints(key, info),
                # Set on the last field of a credential pair: the popup renders a
                # Validate button + verdict for that environment there.
                "verify": _verify_spec(key),
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
        # Whether each key pair has been proved to WORK. Read from the EFFECTIVE
        # settings: the pair the bot would trade with lives in the account file (or
        # .env), and a bare ``Settings()`` would report every stored key as unset.
        #
        # Deliberately NOT rendered by the popup when it opens: a stored verdict
        # next to a masked (or empty) box reads as a bug — see `credentialRow` in
        # app.js. The row shows only the answer to a check the operator asked for.
        # This stays in the payload for API consumers and for debugging.
        "credentials": credentials_mod.all_checks(get_effective_settings()),
    }


def verify_credentials(env: str, key_id: Optional[str] = None, secret: Optional[str] = None) -> dict:
    """Check one environment's credentials against Alpaca, on demand.

    ``key_id``/``secret`` are the values currently in the form, and they are exactly
    what gets checked: the Validate button reports on the boxes the operator is
    looking at, never on the stored pair behind them. An empty box is therefore
    "nothing to validate" rather than "reuse what is saved" — pressing Validate on an
    empty form used to fall back to the stored credential and announce a rejection of
    a key that was not on screen.
    """
    settings = get_effective_settings()
    result = credentials_mod.verify(
        settings,
        env,
        force=True,
        # The mask is this layer's convention, not the checker's: a box showing
        # "********" holds no value to check, and saying "nothing to validate" is
        # more useful than letting Alpaca reject asterisks.
        key_id=_form_value(key_id),
        secret=_form_value(secret),
    )
    return {
        "ok": bool(result["ok"]),
        "message": result["message"],
        "result": result,
        "credentials": credentials_mod.all_checks(settings),
    }


def _form_value(raw: Optional[str]) -> str:
    """A submitted credential box as a checkable value (``""`` when there is none)."""
    if raw is None or raw in ("", MASK):
        return ""
    return str(raw).strip()


def verify_new_credentials(skip_envs=()) -> Dict[str, dict]:
    """Verify any credential pair that has no passing verdict yet.

    Called straight after an account save: a pair added (or changed) just now is
    checked at the moment it is added, while an unchanged pair that already passed
    costs no call at all. ``skip_envs`` names what the save has already checked.
    """
    return credentials_mod.verify_new(get_effective_settings(), skip_envs=skip_envs)


def update_account(values: Dict[str, str]) -> dict:
    """Validate + persist the account settings into ``account.json``.

    Secret fields submitted empty / masked keep their stored value, exactly like
    the global form, so an unedited password field can never wipe a credential.

    A credential pair can fail without failing the save. Only a pair the broker
    *rejects* is held back - storing a key that Alpaca has just refused would make
    the account file claim something untrue, and the operator would meet that
    failure later, somewhere less obvious. Everything else in the form is saved: a
    bad key pair is a reason to keep that pair, not a reason to discard the data
    folder, the S3 settings and the backtest defaults that came with it. A pair that
    merely could not be reached (no network, an unexpected answer) IS saved, because
    "we could not ask" is not evidence of anything.
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

    # Pairs this save actually changes are checked BEFORE anything is written, so a
    # rejection can hold that pair back instead of having to undo it afterwards.
    changed = _changed_pairs(merged, stored)
    verifications = _check_changed_pairs(settings, changed)
    unsaved: Dict[str, str] = {}
    for env, result in verifications.items():
        if not result.get("rejected"):
            continue
        unsaved[env] = result["message"]
        logger.warning("Account save: %s credentials rejected, NOT saved", env.upper())
        for key in _VERIFY_PAIRS[env]:
            # Keep whatever was there. A pair that never worked must not replace one
            # that might, and an empty box stays empty rather than holding a secret
            # the broker has already refused.
            if _text(stored.get(key, "")):
                merged[key] = stored[key]
            else:
                merged.pop(key, None)

    path = account_mod.save_account(settings, merged)
    invalidate()  # the account layer feeds resolve_effective()
    logger.info("Account settings saved to %s", path)

    # Anything still unproven - a pair from .env, say - is checked now that it is in
    # force. The environments already checked above are skipped: asking twice costs a
    # round trip and can only repeat an answer.
    try:
        for env, result in verify_new_credentials(skip_envs=set(changed)).items():
            verifications.setdefault(env, result)
    except Exception:  # noqa: BLE001 - verification must never fail a save
        logger.exception("Credential verification after save failed")

    skipped = ", ".join(sorted(env.upper() for env in unsaved))
    message = f"Saved {len(merged)} setting(s) to {path.name}"
    if unsaved:
        message += f" — {skipped} credentials were rejected and NOT saved"
    elif any(r.get("checked") and r.get("ok") for r in verifications.values()):
        checked_ok = ", ".join(sorted(env.upper() for env, r in verifications.items() if r.get("checked") and r.get("ok")))
        message += f" — {checked_ok} credentials verified"

    return {
        "ok": True,
        "message": message,
        "errors": [],
        "file": str(path),
        "groups": account_sections(Settings()),
        "verifications": verifications,
        # The pairs the broker refused, by environment, so the popup can say what was
        # left behind instead of implying the whole form failed.
        "unsaved_pairs": unsaved,
    }


def _changed_pairs(merged: Dict[str, str], stored: Dict[str, str]) -> Dict[str, tuple]:
    """The credential pairs this submission changes, as complete pairs.

    A pair is checkable only when both halves are non-empty, so a half-typed pair is
    skipped rather than reported as broken, and an unchanged pair is skipped so a
    save never re-asks Alpaca about a credential it already has an answer for.
    """
    changed: Dict[str, tuple] = {}
    for env, (key_key, secret_key) in _VERIFY_PAIRS.items():
        candidate = (_text(merged.get(key_key, "")), _text(merged.get(secret_key, "")))
        if not candidate[0] or not candidate[1]:
            continue
        if candidate == (_text(stored.get(key_key, "")), _text(stored.get(secret_key, ""))):
            continue
        changed[env] = candidate
    return changed


def _check_changed_pairs(settings, changed: Dict[str, tuple]) -> Dict[str, dict]:
    """Check the pairs a save is about to write. Never raises.

    The check is what decides whether a pair is worth storing, so it runs before the
    file is touched — which also means it must not be able to take the save down with
    it. A pair whose check blew up is simply unproven, and unproven pairs are saved:
    the operator's own values are not the thing to sacrifice to a broken check.
    """
    out: Dict[str, dict] = {}
    for env, pair in changed.items():
        try:
            out[env] = credentials_mod.verify(settings, env, force=True, key_id=pair[0], secret=pair[1])
        except Exception:  # noqa: BLE001 - a save must survive a failing check
            logger.exception("Pre-save check of %s credentials failed", env.upper())
    return out


def _human_label(key: str) -> str:
    return key.replace("_", " ").title()


def _text(value: Any) -> str:
    """A submitted or stored value as a trimmed string (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _mask_secret(text: str, sensitive: bool) -> str:
    """Hide a secret wherever it would otherwise be sent to the browser.

    Used for BOTH the field's value and its ``default_value``: the schema describes
    what is in force, so a stored key would otherwise be readable straight out of the
    account popup's payload — and the whole point of the mask is that a re-opened form
    can submit it back unchanged without ever knowing it.
    """
    return MASK if (sensitive and text) else text


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)
