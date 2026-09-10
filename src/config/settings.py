"""Central configuration for the TRAIDER bot.

Every value is loaded from the environment / `.env` file via Pydantic
(BaseSettings). There are NO hardcoded values anywhere else in the bot —
all modules read from this object. See `.env.example` for the full
reference of variables and their defaults.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    historical_bar_size: str = Field(default="1d", description="Candle size to fetch (1h, 4h, 1d, ...)")
    historical_start_date: Optional[str] = Field(default="2022-01-01", description="Fetch history from (YYYY-MM-DD)")
    historical_end_date: Optional[str] = Field(default=None, description="Fetch history until (YYYY-MM-DD); empty = now")
    historical_lookback_years: Optional[int] = Field(
        default=2, ge=1, le=5,
        description="Fetch history for the last N years (1-5) up to now; overrides HISTORICAL_START_DATE",
    )
    backtest_start_date: Optional[str] = Field(default=None, description="Backtest window start; empty = historical start")
    backtest_end_date: Optional[str] = Field(default=None, description="Backtest window end; empty = historical end")
    train_test_split: float = Field(default=0.8, ge=0.0, le=1.0, description="Fraction of data used for training")
    live_lookback_days: int = Field(
        default=10, ge=1,
        description="Lookback window (days) fetched by the daily live poll; should cover FEATURES_MIN_LOOKBACK",
    )
    historical_data_dir: str = Field(
        default="data/historical",
        description="Canonical Parquet dataset directory (one file per symbol/interval)",
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
        default="strategies/active.json",
        description="JSON file with the rule-based strategy (MODEL_TYPE=rule_based)",
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

    # ── Execution (IBKR) — order placement only ──────────────────────
    ibkr_api_url: str = Field(default="https://api.ib.com")
    ibkr_account_id: Optional[str] = None
    ibkr_username: Optional[str] = None
    ibkr_password: Optional[str] = None
    paper_trading: bool = True
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
    def backup_providers(self) -> List[str]:
        """Fallback OpenBB providers parsed from `OPENBB_BACKUP_PROVIDERS`."""
        return [x.strip() for x in self.openbb_backup_providers.split(",") if x.strip()]

    @property
    def momentum_periods(self) -> List[int]:
        """Momentum lookbacks parsed from `FEATURES_MOMENTUM_PERIODS`."""
        return self._parse_int_list(self.features_momentum_periods)

    @staticmethod
    def _parse_int_list(value: str) -> List[int]:
        return [int(x.strip()) for x in value.split(",") if x.strip()]

    # ── Validation ───────────────────────────────────────────────────

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
        "feature_sma_enabled", "feature_rsi_enabled", "feature_atr_enabled",
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


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loads .env once per process)."""
    return Settings()
