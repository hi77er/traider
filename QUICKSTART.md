# TRAIDER Quick Start Guide

## TL;DR: The Plan

Build a modular Python trading bot for AAPL (Apple) stock that:
1. Checks price once a day (during market hours)
2. Uses AI/ML to decide buy/sell/hold
3. Runs on AWS Lightsail with Docker
4. Never loses more than you allow (risk management)
5. Logs everything so you can see why it traded

**Time to Live:** ~5-6 weeks of development + 1-2 weeks paper testing

---

## Phase Breakdown (What to Do When)

### Week 1: Foundation (Days 1-5)
**What:** Build the scaffolding
- [x] Create folder structure
- [x] Install dependencies
- [x] Create Dockerfile with Java (IBKR needs it for order execution; market data comes from OpenBB)
- [x] Setup AWS Lightsail docs

**Deliverable:** Runnable, empty bot structure

### Week 2: Plumbing (Days 6-12)
**What:** Build all the modules (no AI yet)
- [x] Config loader (reads credentials from .env)
- [x] Data fetcher (grabs price history)
- [x] Feature calculator (converts price → numbers for AI)
- [x] State saver (DynamoDB to remember position)
- [x] Logging & alerts (Web Portal when something happens)

**Deliverable:** Bot can fetch data and log it

### Week 3: Strategy (Days 13-16)
**What:** Add the AI and backtest it
- [x] Simple signal generator (logistic regression)
- [x] Backtester (test strategy on old data)
- [x] Risk manager (won't trade if too risky)
- [x] Order executor (actually place trades)
- [x] Wire it all together

**Deliverable:** Strategy validated on historical data (Sharpe ≥ 1.0)

### Week 4: Testing (Days 17-24)
**What:** Make sure it doesn't crash before real money
- [x] Unit tests (test each part separately)
- [x] Integration tests (test all parts together)
- [x] Paper trading (fake money on real IBKR account for 1-2 weeks)

**Deliverable:** Bot runs 1-2 weeks without crashing, P&L is accurate

### Week 5: Deployment (Days 25-30)
**What:** Package it for AWS
- [x] Build Docker image locally
- [x] Deploy to AWS Lightsail
- [x] Setup monitoring & backups
- [x] Health checks so bot auto-restarts if it dies

**Deliverable:** Bot running live on AWS

### Week 6+: Live Trading & Iteration
**What:** Make money (or learn why you shouldn't)
- [x] Start with 1% of capital
- [x] Monitor daily
- [x] Retrain model monthly if needed
- [x] Iterate strategy

---

## The 10 Modules (What Each Does)

```
1. CONFIG → Stores credentials, time interval, risk limits
   └─ Everything else reads from here (no hardcoding)

2. DATA LAYER → Fetches price data from OpenBB Platform
   ├─ Historical: Past data for backtesting
   └─ Live: Current price every 4 hours

3. FEATURES → Converts price → AI-friendly numbers
   └─ (SMA, RSI, momentum, volatility, etc.)

4. MODEL → The "AI" - takes features, outputs BUY/SELL/HOLD
   └─ Trained offline, saved to file, loaded at startup

5. RISK MANAGER → Vetoes risky signals
   ├─ Position sizing (how much to buy?)
   ├─ Circuit breaker (stop if losing too much)
   └─ Risk validator (says YES/NO to each trade)

6. EXECUTION → Only module that places real orders
   └─ Talks to IBKR API (execution only; data comes from OpenBB), retries if order fails

7. STATE TRACKER → DynamoDB table of positions & P&L
   └─ Survives if bot crashes

8. SCHEDULER → Main loop (every 4 hours: fetch → think → trade)
   └─ Uses APScheduler

9. LOGGING & WEB PORTAL → Records every decision + dashboard alerts
   └─ Why did it trade? Why didn't it? Progress, charts, config, alerts.

10. BACKTESTER → Test strategy on historical data before live
    └─ Gate to live: must show Sharpe ≥ 1.0
```

---

## Directory Structure You'll Create

```
traider/
├── src/
│   ├── config/
│   ├── data/
│   ├── features/
│   ├── model/
│   ├── risk/
│   ├── execution/
│   ├── state/
│   ├── scheduler/
│   └── logging/
├── backtest/
├── tests/
├── docker/
│   └── Dockerfile
├── main.py  ← Entry point
├── requirements.txt
├── .env.example
└── TRAIDER_PLAN.md  ← Full details
```

---

## Critical Decisions (Already Made)

✅ **Polling (once a day)** not Websocket  
→ Daily decision cadence (DECISION_INTERVAL_HOURS=24), trades once a day after market close

✅ **DynamoDB (managed)** not PostgreSQL  
→ DynamoDB is managed, durable, scales without operations; supports PITR and export to S3

✅ **Simple AI (Logistic Regression)** first, not neural nets  
→ Fast, debuggable, good enough edge

✅ **Backtest before live** (non-negotiable)  
→ Validates strategy works before risking real money

✅ **OpenBB for data, IBKR for execution**  
→ OpenBB aggregates market data (free providers, no broker auth); IBKR only places orders (OpenBB can't execute)

✅ **Docker + AWS Lightsail**  
→ Reproducible, manageable, ~$15/month

✅ **Web Portal (FastAPI) for monitoring** not Telegram  
→ Dashboard shows status, candlestick chart, and data table; no external messenger

---

## Run the Web Portal

```bash
# one-time: fetch the initial historical dataset (or use the portal's button)
.venv/bin/python -m scripts.backfill

# start the dashboard (configurable in .env via WEB_PORTAL_*)
.venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000
# → open http://localhost:8000
```

Endpoints:
- `GET /` — dashboard (left: summary, chart, Daily Delta, data table, backfill; right: collapsible `.env` settings form)
- `GET /api/v1/health` — health check
- `GET /api/v1/dataset/status` — dataset summary + backfill job state
- `GET /api/v1/dataset/data?start=&end=&limit=&offset=` — paginated OHLCV rows (`limit=0` = all for the chart)
- `POST /api/v1/dataset/backfill` — start the background initial download
- `GET  /api/v1/chart/indicators` — overlay-ready indicator series (price SMA/Bollinger overlays + RSI/ATR/momentum/volatility oscillator panes); memoized server-side
- `GET  /api/v1/delta/status` — dataset sync state (missing completed days + last 5 bars)
- `POST /api/v1/delta/sync` — fetch the missing days into the dataset (Daily Delta panel)
- `GET  /api/v1/config` — editable config schema (sections/fields, secrets masked)
- `POST /api/v1/config` — save form values to `.env` (atomic, revalidated)

The right-hand **Settings (.env)** pane shows every config var grouped by section and is
collapsed by default (toggle `+` to expand). `INSTRUMENT` is **read-only** — it only reflects
`.env` and must be changed there manually, so the trading symbol can't be switched in-flight.
Booleans render as on/off switches. The **Features** section lists the Feature Engineering indicators
(`FEATURE_*_ENABLED`: SMA, RSI, ATR, Bollinger, Momentum, Volatility) to turn each on or off; their
window/period parameters live under **Feature Parameters** (`FEATURES_*`). Secrets (`*_PASSWORD`, `*_API_KEY`, …) are masked — leave the field empty to keep
the existing value. Saving writes `.env` (created if missing) and validates against the Pydantic
`Settings` model; restart the bot to apply.

The **Daily Delta** panel (left, shown once a dataset exists) checks the Parquet for missing
completed days. When synced it shows "All data synced" + the last 5 bars; when days are missing
it lists them and offers **Fetch missing days** (writes them into the dataset).

Daily schedule (all HH:MM in `MARKET_TIMEZONE`): `DECISION_TIME` (signal/decision) and
`DATA_DELTA_PULL_TIME` (pull the completed bar into the dataset, shortly after close).

Auth: set `WEB_PORTAL_AUTH_ENABLED=true` (plus `WEB_PORTAL_USERNAME`/`WEB_PORTAL_PASSWORD`) to require login. Default (empty password) is open for local dev.

---

## Success Criteria at Each Gate

### After Week 1 (Setup)
- [ ] Project builds locally
- [ ] Dockerfile builds
- [ ] Can import all packages

### After Week 2 (Core Modules)
- [ ] Can fetch historical data via OpenBB Platform
- [ ] Can compute features on sample data
- [ ] DynamoDB table exists & persists (traider-state)

### After Week 3 (Strategy)
- [ ] Backtest shows Sharpe ≥ 1.0
- [ ] Backtest shows max drawdown ≤ 25%
- [ ] Backtest shows win rate ≥ 55%

### After Week 4 (Testing)
- [ ] 2+ weeks paper trading, zero crashes
- [ ] P&L accurate within 0.1%
- [ ] All unit tests pass

### After Week 5 (Deployment)
- [ ] Bot running on AWS
- [ ] Logs flowing to CloudWatch
- [ ] Health check passing

### After Week 6+ (Live)
- [ ] Position sizes correct
- [ ] Trades execute on first attempt 90%+ of time
- [ ] Daily P&L within 2% of expected

---

## Build Order (Respect Dependencies)

Don't build in random order! Follow this:

1. **Setup** (config, data, features, state, logging)  
   → These are independent, build in parallel if you want

2. **Backtest framework**  
   → Depends on features, state, logging

3. **Simple model**  
   → Depends on features

4. **Risk & Execution**  
   → Depends on config, state

5. **Scheduler**  
   → Depends on all of the above

6. **Main entry point**  
   → Depends on scheduler

7. **Tests**  
   → Test everything you built

8. **Docker & AWS**  
   → Deploy after testing

9. **Paper trading**  
   → Real IBKR account (paper money) for 1-2 weeks

10. **Go live**  
    → Start with 1% position size

---

## Key Files to Create First

```bash
# Day 1
mkdir -p traider/src/{config,data,features,model,risk,execution,state,scheduler,logging}
mkdir -p traider/{backtest,tests,docker}
touch traider/.env.example
touch traider/requirements.txt
touch traider/main.py

# .env.example (template, no secrets)
# ⚙️ EVERYTHING is configurable here — instrument, trading period,
#    historical fetch window, features, model, risk, gates, execution.
#    See .env.example for the full list of ~50 variables.

# Instrument & trading period
INSTRUMENT=AAPL
DECISION_INTERVAL_HOURS=24
TRADING_START_HOUR=09:30
TRADING_END_HOUR=16:00
MARKET_TIMEZONE=America/New_York

# Market data (OpenBB Platform)
OPENBB_PROVIDER=yfinance        # free provider, no key needed (options: polygon, fmp, tradier...)
# OPENBB_API_KEY=               # only for premium providers

# Historical data period
HISTORICAL_BAR_SIZE=1d
HISTORICAL_START_DATE=2022-01-01
HISTORICAL_END_DATE=

# Features (signal evaluation)
FEATURES_SMA_PERIODS=10,20,50
FEATURES_RSI_PERIOD=14
FEATURES_ATR_PERIOD=14

# Risk management
RISK_LIMIT_PERCENT=2
MAX_LOSS_PERCENT=10
MAX_CONSECUTIVE_LOSSES=3

# Execution (IBKR Web API)
IBKR_API_URL=https://api.ib.com
IBKR_ACCOUNT_ID=YOUR_ACCOUNT_ID
IBKR_USERNAME=YOUR_USERNAME
IBKR_PASSWORD=YOUR_PASSWORD  # ← Will be loaded from AWS Secrets Manager in prod
PAPER_TRADING=True

# Web Portal & state
WEB_PORTAL_ENABLED=True
WEB_PORTAL_PORT=8000
WEB_PORTAL_USERNAME=admin
DATABASE_PATH=./traider.db
```

---

## Common Pitfalls (Avoid These)

❌ **Don't hardcode credentials**  
→ Use .env + AWS Secrets Manager

❌ **Don't skip backtesting**  
→ Backtest everything before live

❌ **Don't start with huge positions**  
→ Start with 1% of capital, increase after validation

❌ **Don't ignore logging**  
→ You'll need detailed logs to debug issues

❌ **Don't assume perfect execution**  
→ Network fails, orders get rejected, build retry logic

❌ **Don't skip paper trading**  
→ 1-2 weeks on paper catches 80% of bugs

---

## Testing Levels (Required)

### Unit Tests (One module at a time)
```python
test_config.py       # Config loads correctly
test_data.py         # Data fetchers work with mocks
test_features.py     # Feature computation correct
test_model.py        # Signal generation works
test_risk.py         # Risk checks work
test_execution.py    # Order building works
test_state.py        # State persists correctly
```

### Integration Tests (All modules together)
```python
test_full_loop.py    # Data → Features → Signal → Risk → Execute → State
test_error_cases.py  # What if API fails? Retry? Handle?
```

### Paper Trading (Real account, fake money)
- 1-2 weeks real time
- Zero crashes allowed
- P&L accuracy within 0.1%

---

## Monitoring Checklist (Post-Live)

### Daily
- [ ] Is bot running? (check AWS Lightsail)
- [ ] Any error logs? (check CloudWatch)
- [ ] Did it trade? (check IBKR account)
- [ ] Is P&L reasonable? (±2% of expected)

### Weekly
- [ ] Win rate this week
- [ ] Sharpe ratio this week
- [ ] Compare to backtest expectations

### Monthly
- [ ] Retrain model on latest data
- [ ] Backtest new model
- [ ] Check if market regime changed
- [ ] Update risk limits if needed

---

## Resources Needed

**Accounts:**
- IBKR account (real money or paper — for order execution)
- OpenBB Platform (open-source, free; data only)
- AWS account ($20-50/month for Lightsail)
- Web Portal (FastAPI dashboard — free, open-source)

**Tools:**
- Python 3.11+
- Docker (free)
- Git (free)
- All libraries are open-source (free)

**Time:**
- 200-250 developer hours
- 5-6 weeks calendar time
- 1-2 weeks paper trading (concurrent)

---

## What Success Looks Like

✅ **Code:**
- Modular, testable, documented
- 80%+ unit test coverage
- Zero crashes in paper trading
- Backtest metrics > targets

✅ **Operationally:**
- Bot runs 24/7 on AWS
- Every decision logged
- Alerts work (Web Portal)
- Backups daily to S3

✅ **Financially:**
- Live Sharpe ≥ 1.2
- Win rate ≥ 55%
- Monthly P&L > 2%
- Max drawdown < 20%

---

## Next Action

→ Print this document and the full `TRAIDER_PLAN.md`  
→ Read through architecture overview  
→ Begin Phase 1: Setup (directory structure, dependencies, Dockerfile)  
→ Day 1 target: Have the bot structure built and empty imports working  

---

**Version:** 1.0 - Draft Plan Ready to Execute  
**Last Updated:** 2026-08-20  
**Status:** ✅ Ready to Begin Development
