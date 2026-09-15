"""Central configuration for the TRAIDER bot.

Configuration has THREE layers, and this class is the single merged view of all
of them (see ``account.py``, ``effective.py`` and ``model/rules.py``):

1. **Global** (``.env``) — how this machine reaches the outside world: the data
   provider and its keys, plus internal file paths.
2. **Account** (``settings/account/account.json``) — what is true of this trading
   account: broker credentials (Trading Account), backtest defaults and where
   data/results are stored.
3. **Strategy** (``settings/strategies/store.json``) — how each strategy trades:
   instrument, bar size, features, model, gates, risk limits and schedule.

Precedence: **strategy > account > .env > schema default**. Every module reads a
``Settings`` object, so which layer a value came from never leaks into the rest
of the bot. See ``.env.example`` for the global reference and the two JSON files
for the per-account / per-strategy values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import history


class Settings(BaseSettings):
    """One source of truth for every tunable in the bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )

    # ── Trading instrument & trading period ──────────────────────────
    instrument: str = Field(default="AAPL", description="Stock ticker the strategy operates on")
    decision_interval_hours: int = Field(default=24, ge=1, description="Trading decision period (hours)")
    trading_start_hour: str = Field(default="09:30", description="Start of trading window (HH:MM, exchange-local)")
    trading_end_hour: str = Field(default="16:00", description="End of trading window (HH:MM, exchange-local)")
    market_timezone: str = Field(default="America/New_York", description="Timezone of the exchange where the symbol trades")
    decision_time: str = Field(default="09:45", description="Daily time (HH:MM, exchange-local) the signal is computed / decision made")
    data_delta_pull_time: str = Field(default="16:30", description="Daily time (HH:MM, exchange-local) the delta is pulled into the dataset")

    # ── Market data (OpenBB Platform) ────────────────────────────────
    openbb_provider: str = Field(default="yfinance", description="OpenBB data provider")
    openbb_api_key: Optional[str] = Field(default=None, description="API key for premium OpenBB providers")
    openbb_backup_providers: str = Field(
        default="yfinance,polygon,fmp", description="Comma-separated fallback providers"
    )
    data_cache_enabled: bool = Field(default=True, description="Cache fetched candles locally")
    cache_dir: str = Field(default=".cache", description="Local data cache directory")

    # ── Historical data period (backtest & training) ─────────────────
    historical_bar_size: str = Field(default="1d", description="Candle size to fetch (1m, 15m, 1h, 4h, 1d, ...)")
    historical_start_date: Optional[str] = Field(default="2022-01-01", description="Fetch history from (YYYY-MM-DD)")
    historical_end_date: Optional[str] = Field(default=None, description="Fetch history until (YYYY-MM-DD); empty = now")
    historical_lookback: Optional[str] = Field(
        default="2y",
        description="How much history to fetch, as <N>y years or <N>d days (e.g. 2y, 30d) up to now; "
        "overrides HISTORICAL_START_DATE. Which periods are usable depends on HISTORICAL_BAR_SIZE — "
        "see src/config/history.py",
    )
    backtest_start_date: Optional[str] = Field(default=None, description="Backtest window start; empty = historical start")
    backtest_end_date: Optional[str] = Field(default=None, description="Backtest window end; empty = historical end")
    train_test_split: float = Field(default=0.8, ge=0.0, le=1.0, description="Fraction of data used for training")
    live_lookback_days: int = Field(
        default=10, ge=1,
        description="Lookback window (days) fetched by the daily live poll; should cover FEATURES_MIN_LOOKBACK",
    )
    # ── Where the data lives (account setting) ───────────────────────
    # ONE folder per account; the two subfolders are derived from it so the user
    # configures a single path in Account Settings. Explicit values still win, so
    # a caller (or a test) can point either directory at a scratch location.
    data_dir: str = Field(
        default="data",
        description="Root folder for this account's data; holds the historical/ and backtest/ subfolders",
    )
    historical_data_dir: str = Field(
        default="",
        description="Canonical Parquet dataset directory (one file per symbol/interval); empty = <DATA_DIR>/historical",
    )
    backtest_dir: str = Field(
        default="",
        description="Backtest runs + reports directory; empty = <DATA_DIR>/backtest_results",
    )
    # Optional S3 sync: the dataset is written locally, then uploaded to S3 as
    # the durable source of truth. Disabled (local-only) until deployment.
    s3_enabled: bool = Field(default=False, description="Sync the canonical dataset to S3")
    s3_bucket: str = Field(default="", description="S3 bucket for the dataset")
    s3_prefix: str = Field(default="traider/historical", description="S3 object key prefix")
    s3_endpoint_url: Optional[str] = Field(default=None, description="e.g. MinIO for local dev")

    # ── Features — Feature Engineering toggles (on/off) ──────────────
    # Each indicator produced by the Features Engineering module can be
    # switched on/off. These FEATURE_* keys render in the "Features"
    # section; the FEATURES_* keys below are the window/period parameters.
    feature_sma_enabled: bool = Field(
        default=True, description="Trend indicator over FEATURES_SMA_PERIODS windows"
    )
    feature_ema_enabled: bool = Field(
        default=True,
        description="Trend indicator over FEATURES_EMA_PERIODS windows (weights recent bars more than an SMA)",
    )
    feature_macd_enabled: bool = Field(
        default=True,
        description="MACD — EMA(fast) − EMA(slow), plus its signal line and histogram",
    )
    feature_rsi_enabled: bool = Field(
        default=True, description="Momentum oscillator (0-100)"
    )
    feature_atr_enabled: bool = Field(
        default=True, description="Volatility (average true range)"
    )
    feature_bollinger_enabled: bool = Field(
        default=True, description="Price position within Bollinger Bands (%B)"
    )
    feature_momentum_enabled: bool = Field(
        default=True, description="Return over FEATURES_MOMENTUM_PERIODS windows"
    )
    feature_volatility_enabled: bool = Field(
        default=True, description="Rolling standard deviation of returns"
    )
    feature_vwap_enabled: bool = Field(
        default=True, description="Rolling volume-weighted average price (VWAP)"
    )
    feature_volume_enabled: bool = Field(
        default=True, description="Trading volume vs its rolling average (relative volume)"
    )
    feature_volume_abs_enabled: bool = Field(
        default=True, description="Raw traded volume per candle (absolute volume)"
    )

    # ── Feature parameters (windows/periods) ─────────────────────────
    features_sma_periods: str = Field(default="10,20,50", description="Comma-separated SMA windows")
    features_ema_periods: str = Field(default="9,21,50", description="Comma-separated EMA windows")
    features_macd_fast_period: int = Field(default=12, ge=2, description="MACD fast EMA window")
    features_macd_slow_period: int = Field(default=26, ge=2, description="MACD slow EMA window")
    features_macd_signal_period: int = Field(default=9, ge=2, description="MACD signal-line EMA window")
    features_rsi_period: int = Field(default=14, ge=2)
    features_atr_period: int = Field(default=14, ge=2)
    features_bollinger_period: int = Field(default=20, ge=2)
    features_bollinger_std: float = Field(default=2.0, ge=0.0)
    features_momentum_periods: str = Field(default="10,20", description="Comma-separated momentum windows")
    features_volatility_period: int = Field(default=20, ge=2)
    features_vwap_period: int = Field(default=20, ge=2, description="VWAP lookback window (bars)")
    features_volume_period: int = Field(default=20, ge=2, description="Relative-volume lookback window (bars)")
    features_min_lookback: int = Field(default=50, ge=1, description="Warmup bars before emitting features")

    # ── Model (signal generator) ─────────────────────────────────────
    model_type: str = Field(default="logistic_regression", description="logistic_regression | rule_based")
    model_buy_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    model_sell_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    model_retrain_interval_days: int = Field(default=30, ge=1)
    strategy_rules_file: str = Field(
        default="settings/strategies/store.json",
        description="JSON file holding the strategy store (all strategies + which one is active; MODEL_TYPE=rule_based)",
    )
    account_settings_file: str = Field(
        default="settings/account/account.json",
        description="JSON file holding the account-wide settings (broker, backtest defaults, data folder)",
    )

    # ── Risk management ──────────────────────────────────────────────
    risk_limit_percent: float = Field(default=2.0, ge=0.0, description="% of account risked per trade")
    max_loss_percent: float = Field(default=10.0, ge=0.0, description="Max daily drawdown before circuit breaker")
    max_consecutive_losses: int = Field(default=3, ge=1)
    max_exposure_percent: float = Field(default=100.0, ge=0.0, description="Max % of account in one position")
    position_sizing_mode: str = Field(default="fixed_risk", description="fixed_risk | volatility_target")
    stop_loss_percent: float = Field(default=2.0, ge=0.0)
    take_profit_percent: float = Field(default=4.0, ge=0.0)
    circuit_breaker_enabled: bool = True
    # Replay the SAME risk layer in backtests (sizing, stop/take, circuit
    # breaker) so the Gate measures the system that will actually trade rather
    # than a bare signal replay. Set False for the raw strategy numbers.
    apply_risk_layer: bool = Field(
        default=True,
        description="Replay the risk layer (sizing, stop/take, circuit breaker) in backtests.",
    )
    # When False the model is long/flat only: a SELL closes a long and is
    # ignored while flat. When True a SELL while flat opens a SHORT instead.
    # Either way only ONE position (long OR short) exists at a time: a BUY
    # closes a short, a SELL closes a long, and repeats while a position is
    # open are ignored. Per-strategy toggle in the Configuration panel.
    allow_short: bool = Field(
        default=False,
        description="Open a short by selling, and close it by buying it back.",
    )

    # ── Backtest quality gates ───────────────────────────────────────
    # Each field is bounded so a typo (e.g. a 2-digit Sharpe) can't silently
    # make the Gate pass/fail bar nonsensical. Values are validated by pydantic
    # on every Settings() load AND when the Web Portal saves the global form.
    gate_min_sharpe: float = Field(default=1.0, ge=0.0, le=10.0,
        description="Minimum annualized Sharpe ratio required to pass the Gate (0-10).")
    gate_max_drawdown_percent: float = Field(default=25.0, ge=0.0, le=100.0,
        description="Max equity drawdown (%) allowed to pass the Gate (0-100).")
    gate_min_win_rate_percent: float = Field(default=55.0, ge=0.0, le=100.0,
        description="Minimum win rate (%) over closed trades required to pass (0-100).")
    gate_max_weekly_loss_percent: float = Field(default=5.0, ge=0.0, le=100.0,
        description="Max single-week loss (%) allowed to pass the Gate (0-100).")
    backtest_slippage_percent: float = Field(default=0.05, ge=0.0)
    backtest_commission_per_trade: float = Field(default=0.0, ge=0.0, description="USD per side")

    # ── Execution — order placement only (data always comes from OpenBB) ──
    # ALPACA is the only broker. Its paper and live environments are the SAME API
    # with a different base URL and key pair, so switching between them is a
    # single triple swap (src/execution/config.py).
    # Credentials describe this trading ACCOUNT, so they live in the account
    # layer (settings/account/account.json) — one file shared by every strategy.
    # The MODE (paper vs live) is per-STRATEGY, so a strategy still being
    # developed can run against the paper account while a proven one trades live.
    alpaca_paper_api_key: Optional[str] = Field(default=None, description="Alpaca PAPER API key id")
    alpaca_paper_api_secret: Optional[str] = Field(default=None, description="Alpaca PAPER API secret")
    alpaca_live_api_key: Optional[str] = Field(default=None, description="Alpaca LIVE API key id")
    alpaca_live_api_secret: Optional[str] = Field(default=None, description="Alpaca LIVE API secret")
    execution_env: str = Field(default="paper", description="Which environment orders go to: paper | live")
    execution_max_retries: int = Field(default=3, ge=0)
    execution_retry_base_delay_seconds: float = Field(default=1.0, ge=0.0)
    execution_order_timeout_seconds: int = Field(default=60, ge=1)

    # ── Scheduler ────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scheduler_timezone: str = Field(default="America/New_York")

    # ── State storage (DynamoDB) ─────────────────────────────────────
    aws_region: str = Field(default="us-east-1")
    dynamodb_table: str = Field(default="traider-state")
    dynamodb_ttl_days: int = Field(default=30, ge=0)
    dynamodb_endpoint_url: Optional[str] = Field(default=None, description="e.g. DynamoDB Local")

    # ── Web Portal (dashboard: progress, charts, config, alerts) ─────
    web_portal_enabled: bool = Field(default=True, description="Serve the web dashboard")
    web_portal_host: str = Field(default="0.0.0.0", description="Bind host for the web portal")
    web_portal_port: int = Field(default=8000, ge=1, le=65535, description="Port for the web portal")
    web_portal_auth_enabled: bool = Field(default=True, description="Require login to view the portal")
    web_portal_username: str = Field(default="admin", description="Portal login username")
    web_portal_password: Optional[str] = Field(default=None, description="Portal login password (secret)")

    # ── Parsed helpers ───────────────────────────────────────────────

    @property
    def sma_periods(self) -> List[int]:
        """SMA windows parsed from `FEATURES_SMA_PERIODS`."""
        return self._parse_int_list(self.features_sma_periods)

    @property
    def ema_periods(self) -> List[int]:
        """EMA windows parsed from `FEATURES_EMA_PERIODS`."""
        return self._parse_int_list(self.features_ema_periods)

    @property
    def backup_providers(self) -> List[str]:
        """Fallback OpenBB providers parsed from `OPENBB_BACKUP_PROVIDERS`."""
        return [x.strip() for x in self.openbb_backup_providers.split(",") if x.strip()]

    @property
    def paper_trading(self) -> bool:
        """Derived: True when orders go to the simulated account.

        ``EXECUTION_ENV`` replaced the old ``PAPER_TRADING`` boolean, which could
        not express "paper for a strategy still being developed, live for a
        proven one". Kept as a read-only alias so existing callers keep working;
        it is NOT a Settings field, so it is not editable and never appears in a
        saved run's settings snapshot.
        """
        return self.execution_env == "paper"

    @property
    def momentum_periods(self) -> List[int]:
        """Momentum lookbacks parsed from `FEATURES_MOMENTUM_PERIODS`."""
        return self._parse_int_list(self.features_momentum_periods)

    @staticmethod
    def _parse_int_list(value: str) -> List[int]:
        return [int(x.strip()) for x in value.split(",") if x.strip()]

    # ── Validation ───────────────────────────────────────────────────

    @model_validator(mode="after")
    def _derive_data_dirs(self) -> "Settings":
        """Fill the two data subfolders from DATA_DIR unless set explicitly.

        The Account Settings popup asks for ONE folder; ``historical/`` and
        ``backtest_results/`` are then fixed subfolders of it, so the dataset and
        its backtest runs can never drift into two unrelated places. An explicit
        ``HISTORICAL_DATA_DIR`` (tests, advanced setups) still wins and keeps the
        results beside it.
        """
        root = (self.data_dir or "data").strip().rstrip("/") or "data"
        hist = (self.historical_data_dir or "").strip()
        back = (self.backtest_dir or "").strip()
        if not hist and not back:
            hist, back = f"{root}/historical", f"{root}/backtest_results"
        elif hist and not back:
            back = str(Path(hist).parent / "backtest_results")
        elif back and not hist:
            hist = f"{root}/historical"
        self.historical_data_dir = hist
        self.backtest_dir = back
        return self

    @model_validator(mode="after")
    def _validate_macd_periods(self) -> "Settings":
        """MACD is a fast-minus-slow spread, so fast must be the shorter window.

        Swapping them silently inverts the histogram (and every rule built on
        it), so reject it at load time instead of charting a mirrored indicator.
        """
        fast = self.features_macd_fast_period
        slow = self.features_macd_slow_period
        if fast >= slow:
            raise ValueError(
                "FEATURES_MACD_FAST_PERIOD must be smaller than "
                f"FEATURES_MACD_SLOW_PERIOD (got fast={fast}, slow={slow})"
            )
        return self

    @field_validator("model_type")
    @classmethod
    def _validate_model_type(cls, v: str) -> str:
        if v not in ("logistic_regression", "rule_based"):
            raise ValueError(
                f"MODEL_TYPE must be 'logistic_regression' or 'rule_based', got {v!r}"
            )
        return v

    @field_validator("position_sizing_mode")
    @classmethod
    def _validate_position_sizing_mode(cls, v: str) -> str:
        if v not in ("fixed_risk", "volatility_target"):
            raise ValueError(
                f"POSITION_SIZING_MODE must be 'fixed_risk' or 'volatility_target', got {v!r}"
            )
        return v

    @field_validator("execution_env", mode="before")
    @classmethod
    def _validate_execution_env(cls, v):
        """Normalize the environment. Anything unrecognized RAISES rather than
        silently falling back, because a typo must never be read as 'paper' when
        the user meant live (or the reverse)."""
        val = str(v).strip().lower() if v not in (None, "") else "paper"
        if val not in ("paper", "live"):
            raise ValueError(f"EXECUTION_ENV must be 'paper' or 'live', got {v!r}")
        return val

    @field_validator("decision_time", "data_delta_pull_time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        """Schedule times are HH:MM (24h) in MARKET_TIMEZONE."""
        import re

        if not re.fullmatch(r"([01]?[0-9]|2[0-3]):[0-5][0-9]", v.strip()):
            raise ValueError(f"must be HH:MM in 24h format, got {v!r}")
        return v.strip()

    @field_validator(
        "historical_start_date",
        "historical_end_date",
        "backtest_start_date",
        "backtest_end_date",
        mode="before",
    )
    @classmethod
    def _empty_optional_date_to_none(cls, v):
        """An empty date in .env means unset (e.g. end date = now)."""
        if v is None:
            return v
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "feature_sma_enabled", "feature_ema_enabled", "feature_macd_enabled",
        "feature_rsi_enabled", "feature_atr_enabled",
        "feature_bollinger_enabled", "feature_momentum_enabled", "feature_volatility_enabled",
        "feature_vwap_enabled", "feature_volume_enabled", "feature_volume_abs_enabled",
        mode="before",
    )
    @classmethod
    def _coerce_feature_toggle(cls, v):
        """Accept on/off/true/false/1/0 spellings for the feature toggles."""
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("on", "true", "yes", "1", "y", "t"):
                return True
            if low in ("off", "false", "no", "0", "n", "f"):
                return False
        return v

    @field_validator("historical_lookback", mode="before")
    @classmethod
    def _normalize_history_period(cls, v):
        """Rewrite the history window in its canonical ``<N><y|d>`` spelling.

        The setting is ONE value with its unit, so the window cannot be
        half-specified in two fields that then disagree about which one wins.
        A bare number is read as YEARS: that is what this setting used to mean
        (the retired ``HISTORICAL_LOOKBACK_YEARS``), so a value written before
        the unit existed keeps meaning what it always did. An unreadable value
        raises rather than silently reverting to "2 years" — an operator who
        typed ``30days`` deserves to be told, not quietly given something else.
        """
        if v in (None, ""):
            return None
        normalized = history.normalize_period(str(v))
        if normalized is None:
            raise ValueError(
                f"must be a period like '2y' (years) or '30d' (days), got {v!r}"
            )
        return normalized


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loads .env once per process)."""
    return Settings()
