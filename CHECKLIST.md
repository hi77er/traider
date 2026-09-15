# TRAIDER Development Checklist

Track your progress through all 41 tasks across 9 phases.

> **Plan change:** the unfinished Phase 2 & 3 work (state, logging, alerts, Alpaca connectivity, model training, backtest Gate, report generator) was moved to the new **Phase 5 — Deferred: Core & Backtest Completion**, so **Phase 4 (Risk & Execution) can start now**. Old Phases 5–8 became Phases 6–9.
>
> **✅ Since then:** the **report generator (task 21) is COMPLETE** — it is no longer deferred work. Still open in Phase 5: state / logging / alerts / Alpaca connectivity, the backtest Gate (20) and the parameter-sensitivity sweep.

---

## Phase 1: Setup (Days 1-2)

### Infrastructure Setup
- [ ] **setup-env** (1) — Create directory structure
  - [ ] src/{config,data,features,model,risk,execution,state,scheduler,logging}
  - [ ] backtest/, tests/, docker/
  - [ ] Initialize git repo
  - [ ] Create .gitignore (exclude .env, *.db, venv, __pycache__)

- [ ] **setup-deps** (2) — Install core dependencies
  - [ ] Activate venv
  - [ ] Install: requests, schedule, pydantic, sqlalchemy, python-dotenv, numpy, pandas, scikit-learn, pytest, fastapi, uvicorn, docker
  - [ ] Create requirements.txt
  - [ ] Verify imports work

- [ ] **setup-docker** (3) — Create Dockerfile (Python + OpenBB data layer + Alpaca execution)
  - [ ] Base: Debian slim + Python 3
  - [ ] No sidecar gateway: orders go to Alpaca over HTTPS with an API key
  - [ ] Multi-stage: build the venv, then a minimal runtime image
  - [ ] Copy bot code
  - [ ] Expose port 8000 (web portal)
  - [ ] Create entrypoint.sh (check credentials + start bot)

- [ ] **setup-aws** (4) — Prepare AWS Lightsail config
  - [ ] Document Lightsail container deployment steps (use Terraform)
  - [ ] AWS Secrets Manager for credentials
  - [ ] Provision DynamoDB table for state storage (via Terraform)
  - [ ] CloudWatch logging configuration

**Phase 1 Gate:** Project builds, Docker builds, venv ready

---

## Phase 2: Core Modules (Days 3-8)

### Config Module
- [x] **config-create** (5) — Create config module
  - [x] src/config/settings.py with Pydantic BaseSettings
  - [x] Required vars: OPENBB_PROVIDER (+ optional OPENBB_API_KEY)
  - [x] Required vars (execution): ALPACA_PAPER_API_KEY, ALPACA_PAPER_API_SECRET (plus ALPACA_LIVE_* for live)
  - [x] Instrument & period: INSTRUMENT (AAPL), DECISION_INTERVAL_HOURS, TRADING_START_HOUR, TRADING_END_HOUR, MARKET_TIMEZONE
  - [x] Historical period: HISTORICAL_BAR_SIZE, HISTORICAL_START_DATE, HISTORICAL_END_DATE, BACKTEST_START_DATE, BACKTEST_END_DATE, TRAIN_TEST_SPLIT
  - [x] Features: FEATURES_SMA_PERIODS, FEATURES_EMA_PERIODS, FEATURES_MACD_FAST_PERIOD, FEATURES_MACD_SLOW_PERIOD, FEATURES_MACD_SIGNAL_PERIOD, FEATURES_RSI_PERIOD, FEATURES_ATR_PERIOD, FEATURES_BOLLINGER_PERIOD, FEATURES_BOLLINGER_STD, FEATURES_MOMENTUM_PERIODS, FEATURES_VOLATILITY_PERIOD, FEATURES_MIN_LOOKBACK
  - [x] Model: MODEL_TYPE, MODEL_BUY_THRESHOLD, MODEL_SELL_THRESHOLD, MODEL_RETRAIN_INTERVAL_DAYS
  - [x] Risk: RISK_LIMIT_PERCENT, MAX_LOSS_PERCENT, MAX_CONSECUTIVE_LOSSES, MAX_EXPOSURE_PERCENT, POSITION_SIZING_MODE, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT, CIRCUIT_BREAKER_ENABLED
  - [x] Gates: GATE_MIN_SHARPE, GATE_MAX_DRAWDOWN_PERCENT, GATE_MIN_WIN_RATE_PERCENT, GATE_MAX_WEEKLY_LOSS_PERCENT, BACKTEST_SLIPPAGE_PERCENT, BACKTEST_COMMISSION_PER_TRADE
  - [x] Scheduler: SCHEDULER_ENABLED, SCHEDULER_TIMEZONE
  - [x] Web Portal: WEB_PORTAL_ENABLED, WEB_PORTAL_HOST, WEB_PORTAL_PORT, WEB_PORTAL_AUTH_ENABLED, WEB_PORTAL_USERNAME, WEB_PORTAL_PASSWORD
  - [x] DATABASE_PATH
  - [x] Load from .env file
  - [x] No hardcoded values

### Data Layer
- [x] **data-historical** (7) — Fetch historical candles via OpenBB
  - [x] src/data/historical.py + src/data/openbb_client.py
  - [x] Function: fetch_candles(instrument, start_date, end_date, bar_size)
  - [x] Uses OpenBB Platform SDK (standardized OHLCV output)
  - [x] Output: Pandas DataFrame with OHLCV + volume
  - [x] Handle pagination for large date ranges (provider handles; window from config)
  - [x] Respect provider rate limits (local cache + failover)
  - [x] Cache locally

- [x] **data-live** (8) — Poll current price via OpenBB
  - [x] src/data/live.py
  - [x] Function: get_latest_candle(instrument)
  - [x] Polling at DECISION_INTERVAL_HOURS
  - [x] Error handling with retry + provider failover
  - [x] Cache to avoid excessive API calls
  - [x] Skip polls outside US market hours (weekdays 9:30-16:00 ET)

- [x] **data-tests** (9) — Unit tests for data layer
  - [x] Mock OpenBB API responses
  - [x] Test error handling
  - [x] Verify DataFrame structure
  - [x] Test pagination (covered via windowed fetch; 9 tests passing)

### Features
- [x] **feature-create** (10) — Implement feature engineering
  - [x] src/features/engineering.py (+ indicators.py, schema.py)
  - [x] FeatureEngineer class (compute_frame / compute_latest)
  - [x] SMA (10, 20, 50)
  - [x] EMA (9, 21, 50)
  - [x] MACD (12, 26, 9) — line, signal line and histogram
  - [x] RSI (14)
  - [x] Bollinger Bands (%B)
  - [x] ATR (14)
  - [x] Momentum: (close - close[N]) / close[N]
  - [x] Volatility: rolling std
  - [x] Same logic for backtest and live (compute_latest reuses compute_frame)
  - [x] Per-indicator on/off toggles (FEATURE_*_ENABLED)

- [x] **feature-validation** (11) — Validate consistency
  - [x] Test fixtures with known data
  - [x] Verify output matches expected
  - [x] Backtest vs live comparison (no-lookahead prefix-identity test)
  - [x] tests/test_features.py — 15 tests

**Phase 2 Gate:** All core modules have 80%+ test coverage, data fetchers work

> State (12–13), logging (14) and the alerts feed (15) moved to **Phase 5**.

---

## Phase 3: Strategy & Backtest — COMPLETED (Days 9-12)

### Backtesting
- [x] **backtest-framework** (16) — Build backtester
  - [x] src/backtest/engine.py (+ metrics.py, web service + tests)
  - [x] Simulator: long/flat OR long+short (ALLOW_SHORT), next-bar fills, slippage + commission
  - [x] Takes historical data, features, signal model
  - [x] Output: equity curve, Sharpe, max DD, win rate, trades (Gate report)
  - [x] No actual execution, just simulation

### Model Development
- [x] **model-simple** (18) — Create signal model (rule-based path)
  - [x] src/model/simple_model.py (+ rules.py, settings/strategies/store.json store)
  - [x] Rule-based model (the logistic-regression path is NOT implemented — see Phase 5)
  - [x] Input: candle row + engineered features
  - [x] Output: {signal: BUY/SELL/HOLD, confidence: 0-1}

**Phase 3 status:** task 16 and the rule-based part of 18 are done. The **Gate run (20)** moved to **Phase 5** (it needs a Gate-passing strategy, which is still open). Tasks 17, 19 also moved to Phase 5. The **report generator (21) is now COMPLETE** — see Phase 5.

---

## Phase 4: Risk & Execution (Days 13-15)

### Risk Management
- [x] **risk-position-sizing** (22) — Position sizing  ✅ COMPLETE
  - [x] src/risk/position_sizing.py
  - [x] Formula: (account_size × risk_limit) / stop_loss_distance
  - [x] Capped by MAX_EXPOSURE_PERCENT and by exposure already in use
  - [x] fixed_risk + volatility_target modes (volatility from trailing returns)

- [x] **risk-circuit-breaker** (23) — Circuit breaker  ✅ COMPLETE
  - [x] src/risk/circuit_breaker.py
  - [x] Tracks consecutive losses and the day's realised P&L
  - [x] stop_trading_today() on MAX_CONSECUTIVE_LOSSES / MAX_LOSS_PERCENT
  - [x] Resets on a new day; snapshot()/restore() ready for the state tracker

- [x] **risk-validation** (24) — Unified risk validator  ✅ COMPLETE
  - [x] src/risk/validator.py
  - [x] RiskValidator.validate_signal(signal, state)
  - [x] Checks: size, stop/take sanity, circuit breaker, exposure, shorts allowed
  - [x] Returns (approved, reason) — RiskDecision unpacks to that pair
  - [x] Logs every veto AND every approval (with the size used)

- [x] **risk-backtest-parity** (24b) — apply the risk layer inside the backtest  ✅ COMPLETE
  - [x] src/backtest/risk_sim.py — signals replayed through the same sizing / stop /
        take / breaker rules, with re-entry after a stop-out
  - [x] APPLY_RISK_LAYER (per-strategy, default on) + `inputs.risk` provenance
  - [x] Invariant test: risk layer off == simulate_frame exactly (no drift)
  - [x] Re-ran the Gate so the numbers describe the trading system
  - [x] Chart parity: `signal_service` derives the chart's fills from the SAME risk
        layer, so a stopped-out leg is shaded only to its stop, a breaker-vetoed
        entry is never a fill, and the fill count equals the Backtest panel's trade
        count (verified: 40 = 40 on AMZN 1d). Stop/take exits are marked ✕
        (red stop / green take), vetoed entries are marked `veto`.
  - [x] The held-period band is coloured by the round trip's OUTCOME: green when it
        made money, red when it lost, blue only while a position is still open at the
        end. Each close fill carries `win`/`ret_pct`/`equity_ret_pct` (after sizing),
        on the raw path too.

> **⚠ Gate finding (2026-09-10, AMZN 1d):** with the live risk layer applied the strategy
> goes from **+339% / 85.7% win rate** (raw replay) to **−27% / 20% win rate**, with 32
> stop-outs in 40 trades. The raw number was never achievable — an edge measured with no
> stop does not survive a 2% stop behind a 4% take. Any Gate result taken before 24b is
> therefore meaningless for live. Fix the risk config (wider stop, drop the take, or
> different entries) before trusting a Gate result.

### Execution
- [ ] **execution-alpaca** (25) — Implement the Alpaca executor (execution only)
  - [ ] src/execution/alpaca_executor.py
  - [ ] AlpacaExecutor class
  - [ ] place_order(instrument, side, quantity, stop_loss, take_profit)
  - [ ] Uses the Alpaca Trading API (bracket / OCO orders for the exits)
  - [ ] Resolve paper vs live via src/execution/config.py — never re-derive it
  - [ ] Refuse to send anything while `trading_service.is_trading_on()` is False
  - [ ] Poll for confirmation
  - [ ] Data for decisions comes from OpenBB, not Alpaca

- [x] **trading-switch** — Trading ON/OFF + the configuration lock (done)
  - [x] `data/trading.json` (runtime state, gitignored), always starts OFF
  - [x] Turning it on is refused while the selected account could not place an order
  - [x] A LIVE strategy needs a per-action confirmation, every time
  - [x] HTTP 409 on every configuration write while trading is on: settings,
        account, rules, strategy create/rename/delete/select, backtest, dataset,
        delta, execution env
  - [x] Turning it OFF is always allowed — it is what releases the lock

- [ ] **execution-retry** (26) — Add retry logic
  - [ ] src/execution/retry.py
  - [ ] Exponential backoff: 1s, 2s, 4s
  - [ ] Max retries: 3
  - [ ] Log each attempt
  - [ ] Alert on final failure
  - [ ] Update state on success/failure

**Phase 4 Gate:** Risk module vetoes invalid signals, execution places orders

> **Dependency note:** Phase 4 can start with a simple in-memory position/P&L state; the DynamoDB-backed tracker (task 13) lands in Phase 5, and Phase 6 (Integration) then switches to it.

---

## Phase 5: Deferred — Core & Backtest Completion

The unfinished Phase 2 & 3 work lives here so Risk & Execution can proceed first. These tasks are prerequisites for **Phase 6 (Integration)** and for the backtest Go/No-Go gate.

### Config
- [ ] **config-validation** (6) — remaining item only:
  - [ ] Test the Alpaca paper endpoint connectivity (execution)
  - (done: required env vars, INSTRUMENT format, fail-fast validators, OpenBB connectivity)

### State & Portfolio
- [ ] **state-db-schema** (12) — Design DynamoDB table schema
  - [ ] src/state/schema.py (DynamoDB mapper / boto3 wrapper)
  - [ ] Table: traider-state (items may include positions, portfolio snapshots, trade_history)
  - [ ] Consider single-table design with item types (e.g. PK: "POSITION#<id>", "PORTFOLIO#<ts>")
  - [ ] Define attributes: instrument, entry_price, quantity, entry_time, stop_loss, take_profit, pnl, reason
  - [ ] Set TTL for old state and enable PITR (Point-in-Time Recovery) or configure export to S3
  - [ ] Use boto3, pynamodb or dynamodb-data-mapper for persistence (avoid SQLAlchemy)

- [ ] **state-persistence** (13) — Implement state tracker
  - [ ] src/state/tracker.py
  - [ ] PortfolioTracker class
  - [ ] load_state(), save_state()
  - [ ] update_position(), record_trade()
  - [ ] get_current_pnl(), calculate_drawdown()
  - [ ] ACID properties, handle concurrent access

### Logging & Alerting
- [ ] **logging-setup** (14) — Implement structured logging
  - [ ] src/logging/logger.py
  - [ ] Levels: DEBUG, INFO, WARNING, ERROR
  - [ ] File + console output
  - [ ] Rotate log files daily
  - [ ] Include timestamp, module, decision reason

- [ ] **alerting-setup** (15) — remaining items only (the portal itself is built)
  - [ ] src/logging/alerts.py (alert feed surfaced in the portal)
  - [ ] Alerts on: trades, errors, circuit breaker (surfaced in the portal)
  - [ ] Pages: alerts (feed)
  - [ ] Bot state (scheduler/positions) shown in the portal

### Model & Backtest completion
- [ ] **backtest-dummy-signals** (17) — optional synthetic-signal smoke test (the rule-based model already exercises the engine; do this only if useful)
  - [ ] src/backtest/dummy_signals.py

- [ ] **model-training** (19) — ONLY if the logistic-regression path stays in scope (the app is currently rule-based)
  - [ ] src/model/trainer.py
  - [ ] Train on historical data (2+ years) + train/test split (80/20)
  - [ ] Evaluate: accuracy, F1-score, AUC
  - [ ] Save versioned models (model_v1.pkl)
  - [ ] `model-simple`: Save to pickle/joblib (applies to the trained model only)

- [ ] **backtest-strategy** (20) — run the full backtest and pass the Gate
  - [ ] Run against 2+ years historical data
  - [ ] **GATE CHECK:**
    - [ ] Sharpe ratio ≥ 1.0
    - [ ] Max drawdown ≤ 25%
    - [ ] Win rate ≥ 55%
    - [ ] No week with > 5% loss
  - [ ] If FAIL: iterate the strategy/rules and re-backtest

- [x] **backtest-report** (21) — Generate analysis  ✅ **COMPLETE**
  - [x] Equity curve chart, strategy vs buy & hold benchmark
  - [x] Monthly/yearly returns (monthly matrix + yearly table)
  - [x] Distribution of wins/losses (+ win/loss streaks, hold times)
  - [x] Drawdown path + stats (max / current / longest underwater stretch)
  - [x] The full trade list — every round-trip, no cap
  - [x] Provenance panel ("What was tested": rules + fingerprint, window, costs, gate
        thresholds, effective-settings snapshot) — collapsible, collapsed by default
  - [x] Standalone report page `/report` — run menu per strategy (newest by default,
        click to switch), charts zoom-linked together
  - [x] Entry point: "📊 Open full report" in the Backtest panel header, shown only
        once at least one run is stored — it navigates in the SAME tab
  - [x] "← Back to dashboard" spans the full width of the run menu (top of the left
        column); the empty state uses the same link style
  - [x] "🗑 Delete report" (top-right corner of the first panel) removes the run on
        screen from disk after a confirmation dialog — run file + index record + report
        output. The panel's `latest.json` is repointed at the newest remaining run (or
        removed when none is left), so a deleted report cannot keep showing in the
        Backtest panel. `DELETE /api/v1/report/run?strategy=&run_id=`.
  - [x] Risk layer panel — how the risk layer sized and ENDED every position: stopped
        out / take profit / opposite signal / still open, with count, share, win rate,
        avg + total return and avg bars per exit; the entries the circuit breaker
        refused (bar, side, reason); and the sizing/stop/take/breaker settings actually
        used. Collapsible and collapsed by default (like "What was tested"). Each trade
        row also carries its **Exit** reason. `exits` is derived from the trade rows, so
        it renders for runs recorded before the risk layer too.
  - [x] Reads the FULL run from `data/backtest_results/<strategy>/runs/<run_id>.json`
        (NEVER the trimmed `latest.json`, which is only the panel cache)
  - [x] Tests: `tests/test_report.py` (30) + report/storage tests in `tests/test_backtest.py`

> **Split out of task 21 — still open in Phase 5:** *parameter sensitivity* — a multi-run
> sweep over parameter grids (one stored run per configuration) with a robustness summary
> under `data/backtest_results/<strategy>/reports/`. The report page does not need it; it is
> strategy-hardening work and belongs with the Gate iteration loop (task 20).

**Phase 5 Gate:** Backtest metrics meet targets; state + logging services available for integration.

---

## Phase 6: Integration (Days 16-17)

### Orchestration
- [ ] **scheduler-create** (27) — Implement scheduler
  - [ ] src/scheduler/orchestrator.py
  - [ ] MainOrchestrator class with APScheduler
  - [ ] Loop (every 4 hours):
    1. Fetch live data
    2. Engineer features
    3. Generate signal
    4. Risk check
    5. Execute if approved
    6. Update state
    7. Log
  - [ ] Error handling at each step

- [ ] **scheduler-error-handling** (28) — Robust error handling
  - [ ] Try-except for each scheduler step
  - [ ] Log errors, send alerts
  - [ ] Graceful degradation
  - [ ] Never crash scheduler

- [ ] **main-entry** (29) — Create entry point
  - [ ] main.py
  - [ ] Initialize config
  - [ ] Load models
  - [ ] Start scheduler
  - [ ] Load state
  - [ ] Begin infinite loop
  - [ ] Graceful shutdown handlers (Ctrl+C)

**Phase 6 Gate:** Main loop executes, all components integrated

---

## Phase 7: Testing (Days 18-21)

### Unit Tests
- [ ] **test-unit** (30) — Unit tests for all modules
  - [ ] test_config.py — Config loading & validation
  - [ ] test_data.py — Data fetchers (mock OpenBB)
  - [ ] test_features.py — Feature computation
  - [ ] test_model.py — Signal generation
  - [ ] test_risk.py — Risk checks
  - [ ] test_execution.py — Order execution
  - [ ] test_state.py — State persistence
  - [ ] Target: 80%+ coverage

- [ ] **test-integration** (31) — Integration tests
  - [ ] End-to-end pipeline
  - [ ] Error handling (API down, bad data, rejection)
  - [ ] State updates + persistence
  - [ ] Multiple decision cycles

### Functional Testing
- [ ] **test-paper-trading** (32) — Paper trading 1-2 weeks
  - [ ] Deploy to a real Alpaca account (paper money)
  - [ ] Run for 1-2 weeks live
  - [ ] **GATE CHECK:**
    - [ ] Zero crashes
    - [ ] P&L accuracy ±0.1%
    - [ ] Orders execute correctly
    - [ ] No unexpected behavior
  - [ ] If FAIL: debug and retry

- [ ] **test-load** (33) — Stress test
  - [ ] Simulate 2-3 months decisions in minutes
  - [ ] Verify:
    - [ ] Memory usage stable
    - [ ] DB doesn't corrupt
    - [ ] No race conditions
    - [ ] Scheduler responsive

**Phase 7 Gate:** Paper trading stable for 1-2 weeks. Code quality verified.

---

## Phase 8: Deployment (Days 22-25)

### Docker & Local Testing
- [ ] **deploy-docker-build** (34) — Build Docker locally
  - [ ] Build image: `docker build -t traider .`
  - [ ] Test locally: `docker run traider`
  - [ ] Verify:
    - [ ] Python bot runs
    - [ ] All dependencies installed
    - [ ] Alpaca API reachable (paper endpoint)

### AWS Deployment
- [ ] **deploy-aws-setup** (35) — Lightsail deployment
  - [ ] Create Lightsail container service
  - [ ] Configure ECR repository
  - [ ] Setup environment variables (via Secrets Manager)
  - [ ] Provision DynamoDB table for state storage (via Terraform)

- [ ] **deploy-aws-logging** (36) — CloudWatch logging
  - [ ] Ship logs to CloudWatch
  - [ ] Create log groups
  - [ ] Setup alarms for errors
  - [ ] Create dashboard:
    - [ ] Trade count
    - [ ] P&L
    - [ ] Error rate
    - [ ] Last decision time

- [ ] **deploy-monitoring** (37) — Health checks
  - [x] Implement /health endpoint (GET /api/v1/health in Web Portal)
  - [ ] Lightsail auto-restart if unhealthy
  - [ ] CloudWatch alarm if no decision for 6+ hours
  - [ ] Uptime verification

### Backup & Recovery
- [ ] **deploy-backup** (38) — Configure backups
  - [ ] Configure DynamoDB PITR or periodic export to S3 for archival
  - [ ] Backup model files to S3
  - [ ] Backup config to S3
  - [ ] Document recovery procedure

**Phase 8 Gate:** Lightsail running stable. Monitoring active. Backups working.

---

## Phase 9: Live Trading (Ongoing)

### Go Live
- [ ] **live-go-live** (39) — Switch to live trading
  - [ ] Add the Alpaca LIVE key pair in Account Settings (paper keys are not accepted for live)
  - [ ] Switch the header dropdown to `LIVE — REAL ORDERS` and confirm the pill turns red
  - [ ] Confirm the pill's label carries no `— ⚠ no keys` (that marker = the keys do not resolve)
  - [ ] Turn trading ON from the header switch and accept the confirmation prompt (asked for paper too)
  - [ ] Confirm the switch label flips to `⏹ Turn trading off` and the Trading panel appears under the chart
  - [ ] Confirm the Trading panel appears under the chart and the config/backtest buttons are disabled
  - [ ] Start with 1% of capital
  - [ ] Verify first week:
    - [ ] Orders execute correctly
    - [ ] P&L matches expectations
    - [ ] No unexpected slippage
    - [ ] Alerts working
  - [ ] To change anything at all: turn trading OFF first (the server answers 409 otherwise)

### Monitoring & Iteration
- [ ] **live-monitoring** (40) — Establish monitoring routine
  - [ ] **Daily:**
    - [ ] Check P&L
    - [ ] Review trade log
    - [ ] Look for anomalies
  - [ ] **Weekly:**
    - [ ] Win rate
    - [ ] Sharpe ratio
    - [ ] Max drawdown
    - [ ] Compare vs backtest
  - [ ] **Monthly:**
    - [ ] Full performance review
    - [ ] Check for regime changes

- [ ] **live-iteration** (41) — Iterate based on results
  - [ ] Collect live trading data
  - [ ] Retrain model monthly
  - [ ] Backtest new ideas before deploying
  - [ ] If live Sharpe drops: investigate
  - [ ] Increase position size if confident

**Phase 9 Gate:** First month profitable. Strategy validated. Sustainable.

---

## Daily Status Board

```
Week 1 ███░░░░░░░░░░░░░░░░ 
  Day 1: ☐ setup-env      ☐ setup-deps
  Day 2: ☐ setup-docker   ☐ setup-aws
  Day 3: ☐ config-*       ☐ data-historical
  Day 4: ☐ data-live      ☐ data-tests     ☐ feature-create
  Day 5: ☐ feature-val    ☐ state-*        ☐ logging-*

Week 2 ███░░░░░░░░░░░░░░░░
  Day 6-10: ☐ backtest-framework  ☐ model-simple  ☐ model-training
  **GATE CHECK:** Backtest Sharpe ≥ 1.0? _______

Week 3 ███░░░░░░░░░░░░░░░░
  Day 11-15: ☐ risk-*  ☐ execution-*  ☐ scheduler-*  ☐ main-entry

Week 4 ███░░░░░░░░░░░░░░░░
  Day 16-20: ☐ test-unit  ☐ test-integration
  [Paper Trading ACTIVE]

Week 5 ███░░░░░░░░░░░░░░░░
  Day 21-25: ☐ deploy-docker  ☐ deploy-aws-*
  [Paper Trading ONGOING - Week 1 of 2]

Week 6+ ░░░░░░░░░░░░░░░░░░░
  [Paper Trading ENDS]
  Day 26: ☐ live-go-live  Position Size: _____% Capital
  Ongoing: ☐ live-monitoring  ☐ live-iteration
```

---

## Quality Metrics

### Code Quality
- [ ] Unit test coverage ≥ 80%
- [ ] No linting errors (pylint/flake8)
- [ ] Type hints where applicable
- [ ] Documentation complete

### Testing Results
- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Paper trading 1-2 weeks stable
- [ ] Load test passes

### Performance Targets

**Backtest:**
- [ ] Sharpe ratio ≥ 1.0
- [ ] Max drawdown ≤ 25%
- [ ] Win rate ≥ 55%
- [ ] Avg win / avg loss ≥ 1.5

**Paper Trading:**
- [ ] Zero crashes
- [ ] P&L accuracy ±0.1%
- [ ] Order execution 90%+ first attempt
- [ ] All alerts working

**Live Trading (First Month):**
- [ ] Sharpe ratio ≥ 1.2
- [ ] Win rate ≥ 55%
- [ ] Max drawdown ≤ 20%
- [ ] Monthly return ≥ 2%

---

## Sign-Off

### Completion Checklist
- [ ] All 41 tasks completed
- [ ] All gates passed
- [ ] Code reviewed
- [ ] Tests passing
- [ ] Deployment verified
- [ ] Paper trading successful
- [ ] Go-live approved
- [ ] Monitoring active

### Final Sign-Off
- Developer Name: _________________________
- Date Completed: _________________________
- Go-Live Date: _________________________
- Position Size: _________________________%

---

**Version:** 1.0
**Last Updated:** 2026-08-20
**Total Tasks:** 41
**Status:** Ready to Begin

Print this document and cross off tasks as you complete them!
