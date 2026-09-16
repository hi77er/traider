# TRAIDER: Comprehensive Python AI Trading Bot - Master Plan

## Project Overview
**traider** is a modular, production-grade Python AI trading bot for automated **AAPL (Apple) stock** trading. Market data comes from the **OpenBB Platform SDK**; order execution uses the **Alpaca Trading API**. Deployed on AWS Lightsail.

**Key Characteristics:**
- Rule-based strategy on any instrument (AAPL, NVDA, SXR8.DE …), decided once per
  closed bar of the configured size (1m … 1d)
- Paper and live trading modes
- Fully modular architecture (10 independent modules)
- Backtested strategy validation before live execution
- Full observability: logging, alerts, dashboards
- Durable state persistence (DynamoDB)
- Docker containerized: OpenBB Platform (Python) for data + the Alpaca API for execution (no sidecar gateway, no Java)

---

## Current state (2026-09) — read this first

The parts below are the shape the plan settled into after the implementation. Where
they differ from the module sections further down, **this is the truth**.

**One trading logic, two drivers.** Everything that decides a trade lives in
`src/strategy` and nowhere else:

| File | Role |
|---|---|
| `engine.py` | `StrategyEngine.step(state, bar, act)` — the whole rule set, pure. No I/O, no clock, no fetches |
| `config.py` | `StrategyConfig` — every knob, from one settings object, read by the backtest and a live run alike |
| `state.py` | the position + the last decided bar, serialised so a restart cannot re-fire |
| `broker.py` | the ONLY decide→act seam (`Broker` protocol) |
| `live.py` | `LiveDriver` — a closed bar in, intents out |

The backtest drives the SAME machine (`src/backtest/risk_sim.py` is an adapter onto
it, and `simulate_frame`/`position_intervals` delegate there too), and
`tests/test_strategy_parity.py` fails if the two paths ever disagree about an entry,
an exit, a price, a skipped trade or a broker mismatch. The risks of *this* design
are the ones worth knowing: a fill price is the one legitimate backtest↔live
asymmetry, and a driver must never be allowed to grow a rule of its own.

**Risk settings are optional, and empty means NOT APPLIED.** No master switches.
See Phase 4 and module 5d below.

**Execution is implemented; nothing starts it.** `src/execution` can place a
bracketed order in either environment, and `AlpacaBroker` satisfies the strategy's
broker seam — but `src/scheduler/` is an empty package, so **no order can leave the
process today**. That (plus the deferred loss limits) is the remaining work.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    TRAIDER BOT ARCHITECTURE                 │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐   │
│  │  Config │  │   Data   │  │ Features │  │   Model    │   │
│  │ Module  │  │  Layer   │  │ Engineer │  │   Signal   │   │
│  │         │  │ (OpenBB) │  │          │  │            │   │
│  └────┬────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘   │
│       │            │              │               │           │
│       └────────────┼──────────────┼───────────────┘           │
│                    │              │                           │
│            ┌───────▼──────────────▼───────┐                  │
│            │    Risk Management Module    │                  │
│            │  - Position Sizing           │                  │
│            │  - Circuit Breaker           │                  │
│            │  - Risk Validator            │                  │
│            └───────┬──────────────────────┘                  │
│                    │                                         │
│            ┌───────▼──────────────┐                          │
│            │  Execution Module    │                          │
│            │  - Alpaca Order API  │                          │
│            │  - Retry Logic       │                          │
│            └───────┬──────────────┘                          │
│                    │                                         │
│            ┌───────▼──────────────┐                          │
│            │  State/Portfolio     │                          │
│            │  Tracker (DynamoDB)  │                          │
│            └──────────────────────┘                          │
│                                                               │
│  ┌────────────────────────────────────────────┐             │
│  │     Scheduler/Orchestrator (APScheduler)   │             │
│  │     Main Loop (one decision per new bar)   │             │
│  └────────────────────────────────────────────┘             │
│                                                               │
│  ┌────────────────────────────────────────────┐             │
│  │   Logging & Web Portal (Dashboard)         │             │
│  └────────────────────────────────────────────┘             │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

> **Data flow:** All price data enters via the **OpenBB Platform SDK** (historical + live).
> **Execution flow:** Approved orders are sent via the **Alpaca Trading API**. OpenBB does not place orders.

---

## Directory Structure

```
traider/
├── src/
│   ├── main.py                      # THE TRADING LOOP — a process of its own (see below)
│   ├── process_info.py              # The two process roles: names, banner, the host guard
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py              # Configuration & env validation
│   │   ├── effective.py             # strategy > account > .env resolution
│   │   ├── session.py               # trading window (the decision bound)
│   │   ├── state_files.py           # The atomic JSON read/write pair for runtime state
│   │   ├── trading_state.py         # trading.json — read by the loop AND the dashboard
│   │   └── history.py               # bar size ⇄ history period table
│   ├── data/
│   │   ├── __init__.py
│   │   ├── openbb_client.py         # OpenBB Platform SDK wrapper (provider config, caching)
│   │   ├── historical.py            # Fetch candles for backtesting (via OpenBB)
│   │   ├── dataset.py               # Canonical parquet dataset (read/write/window)
│   │   ├── delta.py                 # What the dataset is missing (tail + interior)
│   │   └── live.py                  # Live candles + the DERIVED required window
│   ├── features/
│   │   ├── __init__.py
│   │   ├── engineering.py           # Feature computation (SMA, RSI, ATR, etc)
│   │   ├── indicators.py            # The pure indicator maths
│   │   └── schema.py                # Feature column names (+ longest window)
│   ├── model/
│   │   ├── __init__.py
│   │   ├── simple_model.py          # Rule-based signal (the only model built)
│   │   └── rules.py                 # Rule/RuleSet models + the strategy store
│   ├── strategy/                    # THE trading logic — one implementation, two drivers
│   │   ├── __init__.py
│   │   ├── engine.py                # strategies the state machine (step/run) + ledger
│   │   ├── config.py                # StrategyConfig: every knob, optional = not applied
│   │   ├── state.py                 # position + last decided bar, serialised
│   │   ├── broker.py                # the ONLY decide→act seam (Broker protocol)
│   │   └── live.py                  # LiveDriver: closed bar in, intents out
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── position_sizing.py       # Size from equity, risk limit, stop distance
│   │   ├── circuit_breaker.py       # Loss limits — NOT wired into a run (deferred)
│   │   └── validator.py             # Unified signal validator — NOT wired into a run
│   ├── execution/                   # The only package that can move money
│   │   ├── __init__.py
│   │   ├── config.py                # paper | live resolution (raises, never downgrades)
│   │   ├── credentials.py           # Are the keys WORKING, not merely present
│   │   ├── alpaca_client.py         # The API as URLs and status codes
│   │   ├── retry.py                 # May this call be tried again?
│   │   ├── alpaca_executor.py       # Place ONE order (bracket, poll, flatten)
│   │   ├── alpaca_broker.py         # AlpacaBroker implements the strategy's Broker
│   │   ├── positions.py             # What each account holds, for the exposure gates
│   │   └── store.py                 # data/live_results/<strategy>/ — the loop's output
│   ├── state/
│   │   ├── __init__.py
│   │   ├── schema.py                # DynamoDB table schema / mapper (boto3/pynamodb)
│   │   └── tracker.py               # Portfolio persistence layer (DynamoDB wrapper)
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── orchestrator.py          # Bar-boundary loop — tick() + run() (Phase 4)
│   ├── logging/
│   │   ├── __init__.py
│   │   ├── logger.py                # Structured logging
│   │   └── alerts.py                # Alert feed (surfaced in the Web Portal)
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py                   # FastAPI app (dashboard — serves HTTP, never trades)
│   │   ├── routes/                  # Progress, charts, config, settings, alerts
│   │   └── auth.py                  # Portal login
│
├── src/backtest/                    # Backtest library (pure: no web imports)
│   ├── __init__.py
│   ├── engine.py                    # Simulation + full-fidelity run results
│   ├── metrics.py                   # Metrics + Gate evaluation
│   ├── report.py                    # Report analytics (monthly/yearly, drawdown, distribution)
│   └── store.py                     # Run/report storage layout
│
│   Output is data, not code — one root for everything the backtester writes:
│     data/backtest_results/<strategy>/latest.json        # trimmed UI view (panel cache)
│     data/backtest_results/<strategy>/index.json         # run menu records
│     data/backtest_results/<strategy>/runs/<run_id>.json # FULL run + inputs (reports read this)
│     data/backtest_results/<strategy>/reports/<run_id>.json  # exports/sweeps (optional)
│
├── tests/
│   ├── __init__.py
│   ├── test_config.py               # Config validation tests
│   ├── test_data.py                 # Data fetcher mocks & tests
│   ├── test_features.py             # Feature engineering tests
│   ├── test_model.py                # Signal model tests
│   ├── test_risk.py                 # Risk module tests
│   ├── test_execution.py            # Order execution tests
│   ├── test_state.py                # State persistence tests
│   ├── test_integration.py          # End-to-end flow tests
│   └── conftest.py                  # Pytest fixtures
│
├── docker/
│   ├── Dockerfile                   # Multi-stage: Python venv build + minimal runtime
│   ├── entrypoint.sh                # Start gateway + bot
│   └── .dockerignore
│
├── scripts/
│   ├── run-bot.sh                   # Start the trading loop      (its own process)
│   ├── run-dashboard.sh             # Start the dashboard         (its own process)
│   └── backfill.py
│
├── .env.example                     # Template for .env (committed, sanitized)
├── .gitignore
├── requirements.txt                 # Python dependencies
├── TRAIDER_PLAN.md                  # This file
└── README.md                        # Quick start, architecture overview
```

**Two processes, sharing files and nothing else** — see README, "Two processes".
`src/main.py` is the trading loop (the bar clock and every order);
`src/web/app.py` is the dashboard (HTTP, never trades). Neither starts or stops the
other, and there is no in-memory state between them: settings and results are files,
and `src/config/effective.py` re-reads them when their mtime changes. The loop host
refuses to start if the dashboard is loaded inside it, and `tests/test_architecture.py`
fails if a web module ever gains a path to the loop.

---

## 10 Core Modules

### 1. **Config Module** (`src/config/settings.py`)
**Responsibility:** Central configuration store for credentials, instruments, intervals, limits, and every strategy parameter. **Everything is configurable via `.env` — no hardcoded values anywhere.**

**Config categories (full reference in `.env.example`):**
- **Trading instrument & period:** `INSTRUMENT`, `TRADING_START_HOUR`, `TRADING_END_HOUR`, `MARKET_TIMEZONE`, `DATA_DELTA_PULL_TIME`
- **Market data:** `OPENBB_PROVIDER`, `OPENBB_API_KEY`
- **Historical data period:** `HISTORICAL_BAR_SIZE`, `HISTORICAL_LOOKBACK`, `HISTORICAL_START_DATE`, `HISTORICAL_END_DATE`, `BACKTEST_START_DATE`, `BACKTEST_END_DATE`, `TRAIN_TEST_SPLIT`
- **Features (signal evaluation):** `FEATURES_SMA_PERIODS`, `FEATURES_EMA_PERIODS`, `FEATURES_MACD_FAST_PERIOD`, `FEATURES_MACD_SLOW_PERIOD`, `FEATURES_MACD_SIGNAL_PERIOD`, `FEATURES_RSI_PERIOD`, `FEATURES_ATR_PERIOD`, `FEATURES_BOLLINGER_PERIOD`, `FEATURES_BOLLINGER_STD`, `FEATURES_MOMENTUM_PERIODS`, `FEATURES_VOLATILITY_PERIOD`, `FEATURES_MIN_LOOKBACK`
- **Features (on/off toggles, one per indicator):** `FEATURE_SMA_ENABLED`, `FEATURE_EMA_ENABLED`, `FEATURE_MACD_ENABLED`, `FEATURE_RSI_ENABLED`, `FEATURE_ATR_ENABLED`, `FEATURE_BOLLINGER_ENABLED`, `FEATURE_MOMENTUM_ENABLED`, `FEATURE_VOLATILITY_ENABLED` (SMA / RSI / ATR / Bollinger / Momentum / Volatility)
- **Model:** `MODEL_TYPE`, `MODEL_BUY_THRESHOLD`, `MODEL_SELL_THRESHOLD`, `MODEL_RETRAIN_INTERVAL_DAYS` — set in `.env`; NOT a strategy-panel section, because rule-based is the only model implemented
- **Risk:** `RISK_LIMIT_PERCENT`, `MAX_LOSS_PERCENT`, `MAX_CONSECUTIVE_LOSSES`, `MAX_EXPOSURE_PERCENT`, `POSITION_SIZING_MODE`, `STOP_LOSS_PERCENT`, `TAKE_PROFIT_PERCENT`, `CIRCUIT_BREAKER_ENABLED`
- **Backtest gates:** `GATE_MIN_SHARPE`, `GATE_MAX_DRAWDOWN_PERCENT`, `GATE_MIN_WIN_RATE_PERCENT`, `GATE_MAX_WEEKLY_LOSS_PERCENT`, `BACKTEST_SLIPPAGE_PERCENT`, `BACKTEST_COMMISSION_PER_TRADE`
- **Execution — account-wide (Alpaca):** `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`, `ALPACA_LIVE_API_KEY`, `ALPACA_LIVE_API_SECRET`, `EXECUTION_MAX_RETRIES`, `EXECUTION_RETRY_BASE_DELAY_SECONDS`, `EXECUTION_ORDER_TIMEOUT_SECONDS`
- **Execution — per strategy:** `EXECUTION_ENV` (paper | live) — stored per strategy but edited from the header dropdown, not a settings panel
- **Execution — runtime (NOT config):** `data/trading.json` holds the trading ON/OFF switch. It is deliberately outside the configuration files, because those are exactly what the switch freezes.
- **Scheduler:** no settings. `SCHEDULER_ENABLED` and `SCHEDULER_TIMEZONE` are RETIRED
  (nothing read either; the trading switch says whether a tick acts, and the schedule
  comes from the exchange clock)
- **State (DynamoDB):** `AWS_REGION`, `DYNAMODB_TABLE`, `DYNAMODB_TTL_DAYS`, `DYNAMODB_ENDPOINT_URL`
- **Web Portal:** `WEB_PORTAL_ENABLED`, `WEB_PORTAL_HOST`, `WEB_PORTAL_PORT`, `WEB_PORTAL_AUTH_ENABLED`, `WEB_PORTAL_USERNAME`, `WEB_PORTAL_PASSWORD`

`settings.py` uses Pydantic `BaseSettings` to load + validate all of these from `.env`. Every other module reads from here.

---

### 2. **Data Layer** (`src/data/`)
**Source:** OpenBB Platform SDK (`from openbb import obb`). OpenBB aggregates many providers (yfinance, polygon, fmp, tradier, etc.) behind a standardized output schema, so the bot code stays provider-agnostic. OpenBB is data-only — it does not place orders (execution lives in Module 6).

**Two sub-components:**

#### 2a. Historical Fetcher (`historical.py`)
- **Purpose:** Pull OHLCV candles for backtesting and model training
- **Inputs:** from config — `INSTRUMENT`, `HISTORICAL_LOOKBACK`, `HISTORICAL_BAR_SIZE` (1m, 15m, 1h, 4h, 1d)
- **Output:** Pandas DataFrame with OHLCV + volume
- **Implementation:** wrap `obb` historical price calls via `openbb_client.py`
- **Features:**
  - Choose provider via `OPENBB_PROVIDER` (free providers first: `yfinance`)
  - Normalize OpenBB's standardized OHLCV output into a uniform DataFrame
  - Handle pagination for large date ranges
  - Respect provider rate limits
  - Cache locally (CSV in `CACHE_DIR`) to avoid re-fetching
  - **Persist to the canonical dataset:** merge into Parquet under `HISTORICAL_DATA_DIR` (e.g. `data/historical/AAPL_1d.parquet`) — this is the stable dataset backtesting/training read via `load_dataset`
  - **S3 sync (deploy):** when `S3_ENABLED` + `S3_BUCKET` are set, each write is uploaded to S3 (durable source of truth) and a missing local dataset is auto-restored — local file stays the fast working copy
  - Support multiple timeframes

#### 2b. Live Fetcher (`live.py`)
- **Purpose:** React to the latest completed bar (every bar, no polling cadence)
- **Inputs:** instrument, lookback_periods (for computing candle)
- **Output:** Single OHLCV candle
- **Implementation:** same OpenBB client as 2a (identical code path for backtest & live)
- **Features:**
  - Simple polling, no websocket
  - Graceful error handling (retry, log)
  - Avoid excessive API calls (cache for interval duration)
  - Provider failover: try backup provider if primary is down
  - **Market hours:** skip polls outside US equity market hours (weekdays ~9:30–16:00 ET); handle overnight/weekend gaps so the latest candle is the last completed bar

---

### 3. **Feature Engineering** (`src/features/`) — ✅ implemented
**Responsibility:** Transform raw OHLCV → feature vector (identical between backtest and live).
Implemented as:
- `indicators.py` — pure pandas SMA / EMA / MACD / RSI (Wilder) / ATR / Bollinger %B / momentum / rolling volatility
- `schema.py` — `active_feature_columns()`: the ordered feature-name contract the model consumes
- `engineering.py` — `FeatureEngineer.compute_frame()` (vectorized) and `.compute_latest()` (same math, last row); per-indicator toggles `FEATURE_*_ENABLED`

**Example Features (all window/period parameters configurable via `.env`):**
- Simple Moving Averages: SMA(10), SMA(20), SMA(50) → `FEATURES_SMA_PERIODS`
- Exponential Moving Averages: EMA(9), EMA(21), EMA(50) → `FEATURES_EMA_PERIODS`
- MACD: EMA(12) − EMA(26), signal EMA(9) → `FEATURES_MACD_FAST_PERIOD`, `FEATURES_MACD_SLOW_PERIOD`, `FEATURES_MACD_SIGNAL_PERIOD` (features `macd_*`, `macd_signal_*`, `macd_hist_*`)
- RSI (Relative Strength Index) → `FEATURES_RSI_PERIOD`
- Bollinger Bands (middle, upper, lower, %B) → `FEATURES_BOLLINGER_PERIOD`, `FEATURES_BOLLINGER_STD`
- ATR (Average True Range) for volatility → `FEATURES_ATR_PERIOD`
- Momentum: (close - close[N periods ago]) / close[N periods ago] → `FEATURES_MOMENTUM_PERIODS`
- Volatility: rolling std → `FEATURES_VOLATILITY_PERIOD`
- Price position: (close - sma20) / atr
- Warmup threshold before emitting features → `FEATURES_MIN_LOOKBACK`

**Key Constraint:** Same feature computation must be used in backtest and live. All parameters come from config — tuning the strategy means editing `.env`, not code.

---

### 4. **Signal/Model Module** (`src/model/`)
**Responsibility:** "AI" core. Takes feature vector → outputs buy/sell/hold + confidence.

#### 4a. Simple Model (`simple_model.py`)
**Start here—don't over-engineer.**
- **Option 1 (Rule-based):** Rules configured via `.env` (e.g., "buy if price < SMA50 and RSI < 30") — see `MODEL_TYPE=rule_based`
- **Option 2 (Logistic Regression):** Train scikit-learn LogisticRegression on historical data (`MODEL_TYPE=logistic_regression`)
- **Output:** `{signal: "BUY"/"SELL"/"HOLD", confidence: 0.75}`
- Signal acts only when confidence meets `MODEL_BUY_THRESHOLD` / `MODEL_SELL_THRESHOLD`
- Retrain cadence from `MODEL_RETRAIN_INTERVAL_DAYS`

#### 4b. Training Pipeline (`trainer.py`)
- Train on historical data (e.g., 2 years)
- Use proper train/test split (e.g., 80/20)
- Evaluate on test set: accuracy, F1-score, AUC
- Save model to `models/model_v1.pkl` (versioned)
- Never deploy if test performance is worse than backtest

---

### 5. **Risk Management** (`src/risk/`)
**Responsibility:** Veto or approve signals based on risk rules.

#### 5a. Position Sizing (`position_sizing.py`)
```
position_size = (account_size × risk_limit_percent) / stop_loss_distance
```
- Mode selectable via `POSITION_SIZING_MODE` (`fixed_risk` | `volatility_target`)
- Risk per trade from `RISK_LIMIT_PERCENT`, default stops from `STOP_LOSS_PERCENT` / `TAKE_PROFIT_PERCENT`
- Ensure no single position > `MAX_EXPOSURE_PERCENT`
- Account for existing open positions

#### 5b. Circuit Breaker (`circuit_breaker.py`)
```
if consecutive_losses >= MAX_CONSECUTIVE_LOSSES OR daily_drawdown > MAX_LOSS_PERCENT:
    STOP_TRADING_TODAY()
```
- Toggle via `CIRCUIT_BREAKER_ENABLED`
- Limits from `MAX_CONSECUTIVE_LOSSES` and `MAX_LOSS_PERCENT`
- Track daily/weekly consecutive losses
- Measure drawdown from session start
- Reset on new day/week

#### 5c. Risk Validator (`validator.py`)
```python
def validate_signal(signal, current_state):
    checks = [
        position_size <= max_exposure,
        not circuit_breaker.is_active(),
        stop_loss_distance > min_distance,
        # ...
    ]
    return all(checks), reason_if_rejected
```
- Single entry point for all risk checks
- Logs every rejection (for debugging)

---

### 6. **Execution Module** (`src/execution/`) — ✅ IMPLEMENTED
**Responsibility:** Only module that places real orders. No execution = no trading.
Nothing outside this package may know `/v2/orders` (asserted by a test).

#### 6a. Alpaca Executor (`alpaca_executor.py`) — ✅ IMPLEMENTED
```python
class AlpacaExecutor:
    def place_order(self, instrument, side, quantity, stop_loss_price, take_profit_price):
        # 1. Resolve paper vs live via src/execution/config.py
        # 2. Refuse first: the trading switch, the numbers, the risk layer
        # 3. Build the order (a bracket/OCO carries the stop + take profit)
        # 4. Submit to the Alpaca Trading API (retrying only what may be retried)
        # 5. Poll for confirmation, then report the fill
```
- **Execution only.** All price data comes from the OpenBB Platform (Module 2); Alpaca is used solely to place and manage orders
- Auth is an API key pair — no gateway, no Java, no session token to refresh
- Paper and live are the SAME API, so switching is one base URL + key pair swap
- Attaches the exits as ONE bracket/OCO order, so a filled take-profit can never
  leave a live stop order behind that opens the opposite position
- Validates order before sending
- Polls order status until filled or timeout

The layer, outermost last — each answers one question and nothing else:

| File | Answers |
|---|---|
| `config.py` | Where would an order go? Raises rather than downgrading |
| `credentials.py` | Are the keys WORKING, or merely present? Asked of Alpaca |
| `alpaca_client.py` | The API in URLs and status codes (auth, timeouts, 404 = flat) |
| `retry.py` | May this call be tried again? Only a failure that never reached a verdict |
| `alpaca_executor.py` | Place ONE order: refuse before sending, bracket the exits, poll |
| `alpaca_broker.py` | `AlpacaBroker` — the `src/strategy/broker.py` protocol, so `LiveDriver` can drive it |

#### 6b. Retry Logic (`retry.py`) — ✅ IMPLEMENTED
```python
def execute_with_retry(fn, max_retries=3, base_delay=1.0, sleep=time.sleep, label="order"):
    # Retries ONLY a failure that never reached a verdict.
```
- Exponential backoff: 1s, 2s, 4s (and `max_retries` counts RETRIES, so 3 means up to
  4 calls — the reading `EXECUTION_MAX_RETRIES` has always had)
- Retried: transport failures, 429, 5xx — the broker saying "not now"
- **Not** retried: 401/403 (a revoked key gets the same answer every time), 4xx
  validation errors (the same wrong order would be resent), and a submit whose
  outcome is unknown — that one is REPORTED, because by then it may be live
- **A retry reuses its `client_order_id`**, which Alpaca deduplicates, so a submit
  that timed out and actually landed cannot double-fill
- Every attempt is logged with the PAPER/LIVE label, because the two accounts look
  identical in Alpaca's portal

---

### 7. **State/Portfolio Tracker** (`src/state/`)
**Responsibility:** Persist position, P&L, trade history to durable DynamoDB (managed) table.

#### 7a. Schema (`schema.py`)
```sql
CREATE TABLE positions (
  id INTEGER PRIMARY KEY,
  instrument TEXT,
  entry_price REAL,
  quantity INTEGER,
  entry_time TIMESTAMP,
  stop_loss REAL,
  take_profit REAL
);

CREATE TABLE portfolio (
  timestamp TIMESTAMP,
  cash REAL,
  total_value REAL,
  realized_pnl REAL,
  drawdown REAL
);

CREATE TABLE trade_history (
  id INTEGER PRIMARY KEY,
  entry_time TIMESTAMP,
  exit_time TIMESTAMP,
  entry_price REAL,
  exit_price REAL,
  pnl REAL,
  signal_reason TEXT
);
```

#### 7b. Tracker (`tracker.py`)
```python
class PortfolioTracker:
    def load_state(self) -> State  # Resume from last run
    def save_state(self, state)    # Persist after each decision
    def update_position(self, instrument, new_state)
    def record_trade(self, entry, exit, pnl)
    def get_current_pnl(self) -> float
    def calculate_drawdown(self) -> float
```

**Why durable?** Bot crashes between decision intervals; state must survive.

---

### 8. **Scheduler/Orchestrator** (`src/scheduler/orchestrator.py`)
**Responsibility:** Main loop tying everything together — one decision per newly generated bar.

```python
class MainOrchestrator:
    def __init__(self, config, data_layer, feature_eng, model, risk_mgr, executor, state_tracker):
        self.scheduler = APScheduler()
    
    def decision_loop(self):
        # 1. Fetch live data
        candle = self.data_layer.get_latest_candle()
        
        # 2. Engineer features
        features = self.feature_eng.compute(candle)
        
        # 3. Generate signal
        signal = self.model.predict(features)
        
        # 4. Risk check
        approved, reason = self.risk_mgr.validate(signal)
        if not approved:
            logger.info(f"Signal rejected: {reason}")
            return
        
        # 5. Execute
        order_id = self.executor.place_order(signal)
        
        # 6. Update state
        self.state_tracker.update(signal, order_id)
        
        # 7. Log
        logger.info(f"Executed: {signal} | Confidence: {signal.confidence}")
    
    def start(self):
        # The TICK is not the cadence: it only has to be frequent enough to notice a
        # new bar. Each run looks at the latest bar's timestamp and decides only if
        # that bar has not been decided on yet, so a slow or restarted process
        # catches up by acting on what is current rather than by replaying.
        self.scheduler.add_job(self.decision_loop, 'interval', minutes=5)
        self.scheduler.start()
```

**Key Features:**
- Graceful error handling in each step (try-except)
- Never crash the scheduler
- Log every decision (approved or rejected)
- Send alerts on errors

---

### 9. **Logging & Alerting** (`src/logging/`)

#### 9a. Logger (`logger.py`)
```python
logger.debug("Feature vector: RSI=45, SMA50=1950")
logger.info(f"BUY signal | Confidence: 0.82")
logger.warning(f"Circuit breaker active, skipping signal")
logger.error(f"Order submission failed: {error_details}")
```
- Structured logs (timestamp, level, module, message)
- File + console output
- Rotate log files daily
- Include all decision rationale for debugging

#### 9b. Web Portal (`src/web/`) — ✅ implemented (dataset stage)
The Web Portal is the single interface for monitoring and controlling the bot. Built with FastAPI + uvicorn (already a dependency via OpenBB).

**Implemented (`src/web/`):**
- `app.py` — FastAPI app, mounts `/static`, includes routers, its own entry point
  (`python -m src.web.app`). **This process serves HTTP and never trades**: order
  placement belongs to the loop (`src/main.py`), which runs separately. See README,
  "Two processes".
- `auth.py` — HTTP Basic auth via `WEB_PORTAL_AUTH_ENABLED` / `WEB_PORTAL_USERNAME` / `WEB_PORTAL_PASSWORD` (dev default: disabled when password empty)
- `routes/pages.py` — `GET /` (dashboard), `GET /api/v1/health`
- `routes/dataset.py` — `GET /api/v1/dataset/status`, `GET /api/v1/dataset/data` (paginated), `POST /api/v1/dataset/backfill` (async background thread)
- `routes/delta.py` — `GET /api/v1/delta/status`, `POST /api/v1/delta/sync` (Daily Delta panel)
- `routes/chart.py` + `services/chart_service.py` — `GET /api/v1/chart/indicators`: overlay-ready indicator series (price SMA/Bollinger overlays + oscillator panes), memoized on (data + feature-config) fingerprint
- `routes/config.py` — `GET/POST /api/v1/config` (read/update the `.env` settings form)
- `services/dataset_service.py` — dataset status/rows/backfill job state (in-process lock, one download at a time)
- `services/delta_service.py` — Daily Delta: missing-day detection + sync (wraps `src/data/delta.py`)
- `services/config_service.py` — config schema (grouped sections/fields), masked secrets, atomic `.env` writes with Pydantic validation
- `data/delta.py` — eligible-date logic (final daily bar after close), provider-grounded missing-day detection, merge missing bars into the Parquet dataset
- `templates/index.html` + `static/app.js` + `static/style.css` — dark dashboard split into two panes: left = summary card, lightweight-charts candlestick chart, Daily Delta card ("All data synced" + last 5 bars, or missing-days list + Fetch button), paginated data table, "no data" state with download button; right = **collapsed-by-default** editable `.env` settings form (Save/Reload, secrets masked, section groups, boolean toggles). `INSTRUMENT` is **read-only** (only changeable by editing `.env` manually) so the trading symbol can't be switched in-flight.

**Next (pending):**
- **Progress / status:** scheduler state, last decision time, position, P&L
- **Charts:** equity curve, price + indicators, trade markers
- **Alerts:** live alert feed (errors, trades, circuit breaker) — `alerts.py` writes alerts that the portal surfaces

```python
def record_alert(level, title, message):
    # Persist alert (state/alert store). The Web Portal reads this feed.
    pass
```
- Alerts are stored/streamed to the portal, not pushed to an external messenger
- Auth via `WEB_PORTAL_AUTH_ENABLED` / `WEB_PORTAL_USERNAME` / `WEB_PORTAL_PASSWORD`
- Async — never blocks the main loop

---

### 10. **Backtester** (`backtest/`)
**Responsibility:** Validate strategy has edge BEFORE it risks money.

#### Key Files:

**`engine.py`:** `Backtester` class
```python
class Backtester:
    def __init__(self, historical_data, feature_engineer, model, risk_mgr):
        pass
    
    def run(self, start_date, end_date):
        # For each candle in history:
        #   1. Compute features
        #   2. Generate signal (no execution, just logging)
        #   3. Apply risk checks
        #   4. If approved, simulate trade (update virtual portfolio)
        #   5. Track P&L
        
        return BacktestResults(
            equity_curve=equity_curve,
            trades=trades,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss
        )
```

**`dummy_signals.py`:** Simple test signals
```python
def simple_sma_crossover(candles):
    # Buy if price crosses above SMA50, sell below SMA20
    # Use to validate backtester works before real model
```

**Build Order:** Data → Backtester (with dummy signals) → Real model → Integrate with execution.

**Gate to Live Trading:** Backtest must show (thresholds configurable via `.env`):
- Sharpe ratio ≥ `GATE_MIN_SHARPE` (default 1.0)
- Max drawdown ≤ `GATE_MAX_DRAWDOWN_PERCENT` (default 25%)
- Win rate ≥ `GATE_MIN_WIN_RATE_PERCENT` (default 55%)
- No single week loss > `GATE_MAX_WEEKLY_LOSS_PERCENT` (default 5%)

Backtest window comes from `BACKTEST_START_DATE` / `BACKTEST_END_DATE` (or the historical range); costs applied via `BACKTEST_SLIPPAGE_PERCENT` and `BACKTEST_COMMISSION_PER_TRADE`.

---

## Execution Timeline

### Phase 1: Setup (Days 1-2)
```
✅ setup-env            → Create directory structure
✅ setup-deps           → Install dependencies, create venv
✅ setup-docker         → Build Dockerfile (Python/OpenBB for data + Alpaca for execution)
✅ setup-aws            → Document Lightsail deployment
✅ config-create        → Config module with Pydantic
✅ config-validation    → Validate env vars, test OpenBB data + Alpaca execution connectivity
```

### Phase 2: Core Modules (Days 3-8) — completed (state/logging deferred to Phase 5)
**Parallel work possible; respect dependencies:**
```
✅ data-historical      → Fetch historical candles
✅ data-live            → Poll current price
✅ data-tests           → Unit tests for data layer
✅ feature-create       → Feature engineering
✅ feature-validation   → Validate feature consistency
⏸️ state-db-schema      → DynamoDB table schema          [deferred → Phase 5]
⏸️ state-persistence    → Portfolio tracker              [deferred → Phase 5]
⏸️ logging-setup        → Structured logging             [deferred → Phase 5]
🔶 alerting-setup       → Web Portal dashboard ✅ (alert feed → Phase 5)
```

### Phase 3: Strategy & Backtest (Days 9-12) — completed (Gate + report deferred to Phase 5)
```
✅ backtest-framework   → Backtester engine (long/flat + optional shorts)
✅ model-simple         → Rule-based model (logistic path not built)
⏸️ backtest-dummy-signals → Synthetic smoke test           [deferred → Phase 5]
⏸️ model-training       → Train on historical data        [deferred → Phase 5]
⏸️ backtest-strategy    → Run full backtest, Gate check   [deferred → Phase 5]
✅ backtest-report      → Report page + full analysis     [deferred → Phase 5, now COMPLETE]
```
**Decision Gate (now in Phase 5):** If the backtest fails the metrics, iterate the strategy and re-backtest.

### Phase 4: Risk & Execution (Days 13-15)
```
✅ risk-position-sizing     → Calculate safe position size
✅ risk-settings-optional   → every risk field optional; EMPTY = NOT APPLIED, no
                              master switch (APPLY_RISK_LAYER and
                              CIRCUIT_BREAKER_ENABLED removed)
✅ risk-backtest-parity     → Gate measures the SAME system that will trade: one
                              shared state machine (src/strategy) drives the backtest
                              and a live run, proven by tests/test_strategy_parity.py
✅ risk-validation          → Unified risk checks (validator.py — built, NOT wired in)
⏸️ risk-loss-limits         → MAX_CONSECUTIVE_LOSSES / MAX_LOSS_PERCENT applied in
                              the execution loop [collected, not applied]
✅ execution-alpaca         → Alpaca order placement (bracket exits, poll, flatten)
✅ execution-retry          → Retry with backoff (only failures that never reached
                              a verdict; a retry reuses its client_order_id)
✅ execution-broker         → AlpacaBroker implements src/strategy/broker.py
```

### Phase 5: Deferred — Core & Backtest Completion
Moved from Phases 2 & 3 so Risk & Execution can start first. Prerequisite for Phase 6 (Integration).
```
⏸️ state-db-schema          → DynamoDB table schema
⏸️ state-persistence        → PortfolioTracker (DynamoDB)
⏸️ logging-setup            → Structured logging
⏸️ alerting-setup           → Alert feed + bot state in the portal
⏸️ config-validation        → remaining: Alpaca endpoint connectivity test
⏸️ model-training           → trainer.py + versioned models (only if the ML path is used)
⏸️ backtest-strategy        → GATE: Sharpe ≥ 1.0, DD ≤ 25%, WR ≥ 55%, no week > 5% loss
✅ backtest-report          → report page `/report`: equity vs buy & hold, drawdown,
                              monthly/yearly returns, trade distribution, full trade list,
                              rules/config provenance; run menu per strategy  ← COMPLETE
⏸️ parameter-sensitivity    → split out of 21: sweep N×M configs, robustness summary → reports/
```
**Phase 5 Gate:** Backtest metrics meet targets; state + logging available for integration.

### Phase 6: Integration (Days 16-17)
```
⏸️ scheduler-create         → The bar-boundary loop
                              [BUILT — src/scheduler/orchestrator.py ticks the gates in
                               order and sleeps to the next boundary. The HOST that runs
                               it (lease, wake, --once) is Phase 5 and is not built yet.]
⏸️ scheduler-error-handling → Robust error handling [with the scheduler]
⏸️ main-entry               → Entry point wiring the live loop. `src/main.py` is now a
                              real host: it names its role, refuses to start if the
                              dashboard is loaded in its process, and exits non-zero
                              while the loop is unbuilt (never a silent no-op). The
                              loop itself is still to be wired into it (Phase 5).
```

**The full design and build order for this phase: [`docs/execution-loop.md`](docs/execution-loop.md).**
That document is authoritative for the loop — the tick order, the gate policy, the
storage layout, and what is deliberately left out. Phases 0 (foundations) and 1 (the
two driver defects) of it are built; 2–8 are not.

### Phase 7: Testing (Days 18-21)
```
✅ test-unit                → Unit tests all modules
✅ test-integration         → End-to-end flow tests
✅ test-paper-trading       → Paper money on a real Alpaca account (1-2 weeks)
✅ test-load                → Stress test 2-3 months in fast time
```
**Go/No-Go Decision:** If paper trading shows issues, debug & iterate.

### Phase 8: Deployment (Days 22-25)
```
✅ deploy-docker-build      → Build & test Docker image locally
✅ deploy-aws-setup         → Lightsail container deployment
✅ deploy-aws-logging       → CloudWatch logs + alarms
✅ deploy-monitoring        → Health checks & auto-restart
✅ deploy-backup            → S3 backups for state & model
```

### Phase 9: Live Trading & Iteration (Ongoing)
```
✅ live-go-live             → Switch to live, start small position
✅ live-monitoring          → Daily/weekly performance reviews
✅ live-iteration           → Retrain model monthly, backtest new ideas
```

---

## Key Design Decisions & Rationale

### 1. **Polling vs Websocket**
- ✅ Polling (4-hour intervals) = simpler, no persistent connection, perfect for non-day-trading
- ❌ Websocket = overkill, adds complexity, more points of failure

### 2. **Model Choice: Start Simple**
- ✅ Rule-based or logistic regression = fast to train, interpretable, testable
- ❌ Neural networks = overfitting risk, slow training, hard to debug
- Upgrade to ML later if simple rules show edge

### 3. **State in DynamoDB (not in-memory)**
- ✅ Survives bot restarts/crashes
- ✅ Queryable for analysis
- ✅ Lightweight, no external DB dependency
- ✅ Easy to backup

### 4. **Modular Architecture**
- Each module has single responsibility
- Easy to unit test
- Easy to replace (swap model, change risk rules, etc.)
- Easy to understand at a glance

### 5. **Backtest Before Live**
- Validates strategy has statistical edge before risking real money
- Catches bugs in feature/signal logic
- Gives confidence in decision logic
- Required gate to production

### 6. **Docker + AWS Lightsail**
- ✅ Containerized = reproducible, easy to deploy
- ✅ Lightsail = managed, cheap (~$10-20/month)
- ✅ No Java runtime and no sidecar gateway — Alpaca is a plain HTTPS API (execution)
- ✅ OpenBB Platform installed in Python venv (data)
- ✅ CloudWatch for logs/alarms
- ✅ S3 for backups

### 7. **OpenBB for Data + Alpaca for Execution**
- ✅ OpenBB Platform aggregates market data providers behind one SDK — no broker auth needed for data, easier provider switching
- ✅ Same OpenBB data path for backtest & live = no data mismatch
- ✅ Alpaca kept only for order placement (OpenBB cannot execute trades)
- ✅ Free providers (e.g. `yfinance`) keep the data cost at $0 during development

---

## Testing Strategy

### Unit Tests (Phase 7)
- Config loading & validation
- Data fetchers (mock OpenBB API responses)
- Feature computation (known data, known output)
- Signal model (classification metrics)
- Risk validators (edge cases: max exposure, circuit breaker)
- Position sizing (boundary conditions)
- Order execution (mock submission, retry logic)
- State persistence (DB CRUD operations)

### Integration Tests
- Data → Features → Signal pipeline
- Signal → Risk check → Execution pipeline
- State update + persistence
- Error handling (one component fails, others survive)

### Paper Trading (Real Account, Fake Money)
- Deploy bot to a real Alpaca account with paper trading enabled
- Run for 1-2 weeks live
- Verify:
  - Orders execute correctly
  - P&L matches expected
  - No crashes
  - Logging complete

### Load/Stress Tests
- Simulate 2-3 months of decisions in minutes
- Verify:
  - Memory usage stable (no leaks)
  - DB doesn't corrupt
  - Scheduler stays responsive
  - No race conditions

---

## Production Checklist (Before Go-Live)

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Paper trading runs 1-2 weeks without issues
- [ ] Load test passes
- [ ] Backtest meets performance gates
- [ ] Docker image builds & runs locally
- [ ] AWS Lightsail deployment tested
- [ ] CloudWatch logs configured & tested
- [x] Web Portal accessible (dataset stage: summary + chart + data table)
- [x] Web Portal settings form (edit/save `.env` with masked secrets; read-only INSTRUMENT; feature on/off toggles)
- [ ] Web Portal showing bot progress and alerts feed
- [ ] State backup to S3 working
- [ ] Health check endpoint responding
- [ ] Alpaca credentials secure (in AWS Secrets Manager, not in code)
- [ ] OpenBB provider key (if any) secure (in AWS Secrets Manager, not in code)
- [ ] .env template created (no secrets committed)
- [ ] README complete with deployment instructions
- [ ] Runbook written (how to restart, manually close position, etc.)

---

## Monitoring & Maintenance (Post-Live)

### Daily
- Check P&L, recent trades
- Review error logs for anomalies
- Verify bot is still running (health check)

### Weekly
- Analyze win rate, Sharpe, max DD for the week
- Compare to backtest expectations
- Review alert log

### Monthly
- Retrain model on latest data (20% recent, 80% historical)
- Backtest new model before deploying
- Analyze regime changes (if live Sharpe < backtest, investigate)

### Quarterly
- Full performance review
- Consider strategy improvements
- Update risk limits based on results

---

## Risk Mitigation

1. **Paper First:** Always paper trade 1-2 weeks before live
2. **Small Position:** Start live with 1% of capital
3. **Circuit Breaker:** Stop trading after N losses (protects against model degradation)
4. **Durable State:** Position/P&L survives restart
5. **Comprehensive Logging:** Debug any issue post-facto
6. **Alerts:** Know immediately if something breaks
7. **Manual Override:** Can always close position manually via the Alpaca dashboard

---

## Success Metrics

### Strategy Performance (Target)
- Sharpe ratio ≥ 1.5
- Win rate ≥ 60%
- Max drawdown ≤ 20%
- Monthly return ≥ 2%

### Code Quality
- Unit test coverage ≥ 80%
- No critical bugs in paper trading
- Log every decision (debug-able)

### Operational
- Uptime ≥ 99% (1 crash/100 days OK)
- Alerts respond within 1 min
- State backups daily
- Model retrains monthly

---

## Next Steps

1. **Immediately:** Review this plan, ask clarifying questions
2. **Day 1:** Start Phase 1 (setup)
3. **End of Week 2:** Complete core modules (Phase 2)
4. **End of Week 3:** Backtest validation (moved to Phase 5 — after Risk & Execution)
5. **End of Week 4:** Paper trading starts
6. **End of Week 5:** Go live (small capital)

---

## Appendix: Alpaca Setup

### In Docker:
```dockerfile
FROM debian:bookworm-slim

# No Java and no sidecar gateway: Alpaca is a plain HTTPS API reached with an
# API key pair, so the runtime image needs nothing but Python.
RUN apt-get update && apt-get install -y python3 python3-venv

# Copy bot code
COPY . /app/traider
WORKDIR /app/traider

# Entrypoint checks for credentials, then starts the bot
COPY docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

### Execution flow:
1. The bot goes through `src/execution/config.py`, which resolves ONE triple:
   base URL + key id + secret (`https://paper-api.alpaca.markets` or
   `https://api.alpaca.markets`) from `EXECUTION_ENV`.
2. Auth is an HTTP Basic header (`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`) on
   every request — there is no login call and no session token to refresh.
3. The binary live gate is at runtime, not in config: a LIVE strategy needs
   `EXECUTION_ENV=live`, a live key pair, **and** `confirm_live: true` on
   `POST /api/v1/trading/on` — asked again every time trading is turned on. (This
   replaced the old `EXECUTION_LIVE_ACK` field: a stored acknowledgement could arm
   real trading with one Save, which is the wrong shape for a gate whose job is to
   make one action deliberate.) Anything missing is REFUSED, never downgraded.
4. Paper and live are the same API, so switching changes nothing else.
5. Trading ON takes a lock: every configuration write answers HTTP 409 until it is
   turned off again, so a strategy cannot be reconfigured mid-flight. The header
   dropdown shows which environment is active; every stored run records it under
   `inputs.execution`.

### Getting a paper account:
1. Sign up at alpaca.markets — a **Paper Only Account** is available worldwide
   with just an email address (no KYC, no funding), and starts with $100k of
   simulated cash.
2. In the dashboard, select the Paper account, then generate its API key pair.
   Paper and live keys are different; a new paper account needs new keys.
3. Put the key/secret in Account Settings (or `.env`), and leave
   `EXECUTION_ENV=paper` until you deliberately want real orders.

### Superseded: IBKR Client Portal Gateway
Dropped in favour of Alpaca. The gateway has **no credential endpoint** — you log
into `https://localhost:5000` once interactively in a browser (with 2FA) and the
gateway holds the session, so the bot cannot authenticate unattended and the
session expires daily. Storing a password does not solve that.

---

## Appendix B: OpenBB Platform Setup

### Install
```bash
pip install openbb          # core platform + default providers
# or:  pip install "openbb[all]"   # all optional providers
```

### Basic usage (data layer)
```python
from openbb import obb

# Historical candles (provider-agnostic output schema)
df = obb.equity.price.historical(
    config.INSTRUMENT,               # "AAPL" — a listed equity, works with all providers
    start_date="2024-01-01",
    end_date="2024-12-31",
    provider=config.OPENBB_PROVIDER,   # e.g. "yfinance"
).to_df()

# Live quote
quote = obb.equity.price.quote(config.INSTRUMENT, provider=config.OPENBB_PROVIDER).to_df()
```
- Standardized output → same code path for backtest & live
- Free providers (e.g. `yfinance`) need no API key; premium ones (`polygon`, `fmp`) need `OPENBB_API_KEY`
- AAPL is directly supported as a plain ticker by all providers (no futures/forex symbol mapping)
- **Market hours:** stock data only exists ~9:30–16:00 ET on weekdays — account for this in live polling and backtest calendar

---

## Useful Resources
- OpenBB Platform Docs: https://docs.openbb.co/platform
- OpenBB GitHub: https://github.com/OpenBB-finance/OpenBB
- Alpaca Trading API Docs: https://docs.alpaca.markets/us/docs/trading-api
- Client Portal Gateway: https://github.com/InteractiveBrokers/cpapi-web-gateway
- APScheduler: https://apscheduler.readthedocs.io/
- SQLAlchemy: https://docs.sqlalchemy.org/
- Pytest: https://docs.pytest.org/

---

**Last Updated:** 2026-09-01  
**Status:** Draft Plan (Ready to Execute)  
**Owner:** Traider Project
