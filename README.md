# TRAIDER

A single-instrument, rule-based trading bot that is developed, backtested and
operated from one place: a FastAPI web portal on top of a pure-Python strategy
stack.

The central design rule is that **the backtest replays the same code the live
bot would run**. Data fetching, feature computation, signal generation,
position sizing, stop/take handling and the circuit breaker all live in
importable modules, and the backtest drives them directly - so a number in the
report describes the system that would actually trade, not a simplified
replay of it.

## Status

| Area | State |
| --- | --- |
| Data pipeline (OpenBB + yfinance, Parquet store, delta backfill) | done |
| Features, rule model, risk layer, backtest engine + Gate | done |
| Web portal (chart, config, backtest panel, report page) | done |
| Tests | 262 passing |
| Live order execution (IBKR) | **not implemented** |
| Portfolio state (DynamoDB) | **not implemented** |
| Scheduler / bot entry point | **not implemented** - `src/main.py` is a stub |

The current strategy **does not pass its own Gate yet** (see
[CHECKLIST.md](CHECKLIST.md) for the metrics). Treat every stored result as
research, not as an expectation.

## How it works

```
OpenBB/yfinance ──> canonical Parquet dataset ──> features ──> rule model
                        (data/historical)             │            │
                                                      └──────> signals
                                                                  │
                        ┌─────────────────────────────────────────┘
                        v
                 RISK LAYER  (sizing -> stop / take -> circuit breaker)
                        │
          ┌─────────────┴──────────────┐
          v                            v
   BACKTEST (next-open fills)     EXECUTION (IBKR) <- not implemented
          │                            │
          v                            v
   report page + run store        portfolio state <- not implemented
```

Two consequences worth knowing:

- **A signal is not a trade.** The risk layer decides whether an entry is
  taken at all (the circuit breaker can veto it), how big it is, and when the
  position actually ends (stop, take profit, or the opposite signal). The
  chart, the backtest panel and the report all read the risk layer's output,
  so they cannot disagree.
- **`APPLY_RISK_LAYER` toggles the entire layer** for a strategy. With it off
  the backtest replays the raw strategy - no sizing, no stops, no halt. That
  mode is useful for attribution ("does my edge survive a stop?") but its
  numbers are not achievable live.

## Requirements

- Python 3.9 (developed and tested on 3.9.6)
- No API key needed for the default setup - the data layer uses yfinance
- An IBKR Client Portal Gateway only for order execution (not required to
  backtest or to use the portal)

## Setup

```bash
python3.9 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env        # the defaults work as-is for a local, data-only run
```

`.env` holds ~50 settings; `.env.example` documents every one of them with
comments. Real values never leave your machine - `.env` is gitignored, and the
template ships with every credential blank.

## Run the portal

```bash
.venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000
# -> http://127.0.0.1:8000
```

Use `.venv/bin/python` explicitly; a bare `python` may resolve to a different
interpreter.

The portal is the whole interface:

- **Strategy bar** - switch, create, rename or delete a strategy (each one has
  its own instrument, bar size, rules and risk settings)
- **Price chart** - candles, indicators, BUY/SELL markers, the held-period
  bands (green when the round trip made money, red when it lost) and the
  stop/take exits
- **Strategy Configuration / Rules / Risk Management** - three collapsible
  panels; the risk panel leads with the two master switches
- **Account Settings** (🏦 header popup) - broker (Trading Account), the single
  data folder, backtest defaults and cloud/state storage; shared by all strategies
- **Global Settings** (⚙ header popup) - the data provider + keys in `.env`
- **Backtest panel** - run the engine, read the Gate and the metrics
- **Historical Delta** - gap-check the dataset against the provider and refill
  missing bars
- **Report page** - the full picture for any stored run: equity vs buy & hold,
  drawdown, monthly/yearly returns, the trade distribution, the exit-reason
  breakdown and the entries the circuit breaker refused
- **Market page** (`/market`, 🌎 header button) - whole-market screening from
  Yahoo Finance: a preset screener, the whole US market, top gainers, highest
  volume, top losers and the small-cap gainers/volume lists. The two long tables
  are collapsible and start collapsed so the page opens as an overview

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 360 passed
```

The suite is offline: OpenBB, the broker and the clock are all stubbed, so it
runs without credentials or market data. The dashboard's and report page's
crosshair-sync logic is exercised by running the real `app.js` / `report.js`
blocks under `node` against fake charts that reproduce lightweight-charts'
actual event semantics (see `tests/test_web/test_crosshair_sync.py`), so it needs
`node` on `PATH`; those tests skip themselves when it is absent.

## Project layout

```
src/
  config/      settings (pydantic) + per-strategy effective settings
  data/        OpenBB client, provider session, screener (Yahoo), dataset, delta backfill
  features/    indicators + feature engineering (no lookahead)
  model/       rule store and rule-based signal generator
  risk/        position sizing, stop/take levels, circuit breaker, validator
  backtest/    engine, metrics + Gate, risk replay, report analytics, run store
  execution/   IBKR executor          (not implemented)
  state/       portfolio tracker      (not implemented)
  scheduler/   decision loop          (not implemented)
  logging/     structured logging, alerts (not implemented)
  web/         FastAPI app, routes, services, templates, static assets
tests/         pytest suite (offline)
settings/      LOCAL DATA (gitignored): strategies/store.json + account/account.json
data/          generated at runtime: historical Parquet + backtest results
```

Runtime data is not in the repository:

```
data/historical/<SYMBOL>_<bar>.parquet         canonical OHLCV
data/backtest_results/<strategy>/latest.json   trimmed view the panel reads
data/backtest_results/<strategy>/runs/<id>.json full, self-describing run
data/backtest_results/<strategy>/index.json    run-menu index
```

A run file is deliberately complete: every equity point, every trade with its
exit reason, and an `inputs` block (settings, rules + fingerprint, window,
costs, risk config) that makes a result reproducible and attributable.

## Configuration

Three layers, each with its own editor in the dashboard:

| Layer | File | Edited from | Holds |
|-------|------|-------------|-------|
| **Global** | `.env` | ⚙ Global Settings | data provider + keys, the paths of the two JSON stores |
| **Account** | `settings/account/account.json` | 🏦 Account Settings | broker (Trading Account), the data folder, backtest defaults, cloud/state storage |
| **Strategy** | `settings/strategies/store.json` | Strategy Configuration / Rules / Risk panels | instrument, bar size, features, model, gates, schedule, risk limits, rules |

Precedence is **strategy > account > .env**, and the process environment still
wins over `.env` (which is why a stray exported variable can silently override
it). Both JSON files are LOCAL DATA: gitignored, and created on first save.

A fresh clone has **no strategy store**: the app starts with an empty one and
the portal shows its "create your first strategy" form. Point
`STRATEGY_RULES_FILE` somewhere else if you keep yours outside the repo.

The Gate thresholds (`GATE_MIN_SHARPE`, `GATE_MAX_DRAWDOWN_PERCENT`,
`GATE_MIN_WIN_RATE_PERCENT`, `GATE_MAX_WEEKLY_LOSS_PERCENT`) are part of the
configuration, not the code - a strategy is judged against the bar you set.

## Documentation

| File | What it is |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | the phased build plan, with progress |
| [CHECKLIST.md](CHECKLIST.md) | task-by-task status, the Gate numbers and the open work |
| [TRAIDER_PLAN.md](TRAIDER_PLAN.md) | the architecture and design decisions |
| [DEPENDENCY_GRAPH.md](DEPENDENCY_GRAPH.md) | module dependency graph |
| [DOCUMENT_INDEX.md](DOCUMENT_INDEX.md) | index of every document |
| [SUMMARY.txt](SUMMARY.txt) | short summary of the project |
| [.github/skills/](.github/skills) | task-scoped notes (OpenBB, backtesting, features, risk, IBKR) |

## Safety

- **Nothing here places an order.** Execution is unimplemented, and the
  intended first live step is paper trading.
- Keep credentials in `.env` (gitignored) or a secret manager - never in code.
- A backtest is not a forecast. The Gate exists to stop a strategy that has not
  earned capital, and passing it is a floor, not a promise.
- This is a personal engineering project, not financial advice.
