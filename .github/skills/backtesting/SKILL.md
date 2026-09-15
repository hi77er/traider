---
name: backtesting
description: 'Backtest the 4-hour strategy on AAPL (Apple) stock before risking money. Use when: implementing backtest/engine.py, computing Sharpe ratio, max drawdown, win rate, avg win/loss; avoiding lookahead/survivorship bias; applying slippage and costs; running dummy signals; or generating the report needed to pass the Gate-2 quality check (Sharpe >= 1.0, DD <= 25%, WR >= 55%).'
---

# Backtesting

The non-negotiable gate before live trading. A strategy may only go live after the backtest meets targets on **unseen data**.

## When to Use
- Implementing `backtest/engine.py`, `backtest/dummy_signals.py`
- Running `backtest-strategy` and `backtest-report` tasks
- Computing/validating metrics (Sharpe, max DD, win rate, avg win/loss, weekly loss cap)
- Building the equity curve and trade log
- Checking for data leakage or execution-naivety
- Deciding "is the edge real?"

## Project Facts
- **Backtest window & thresholds come from `.env`** (edited in the file, not in the dashboard: the settings form was removed): window via `BACKTEST_START_DATE`/`BACKTEST_END_DATE` — both unset by default, which means **the whole period the strategy is configured for**, since a run is triggered by hand rather than on a schedule; gates via `GATE_MIN_SHARPE`, `GATE_MAX_DRAWDOWN_PERCENT`, `GATE_MIN_WIN_RATE_PERCENT`, `GATE_MAX_WEEKLY_LOSS_PERCENT`; costs via `BACKTEST_SLIPPAGE_PERCENT` and `BACKTEST_COMMISSION_PER_TRADE`
- Gate 2 (Phase 3): defaults **Sharpe ≥ 1.0, max DD ≤ 25%, win rate ≥ 55%, no week > 5% loss**
- Uses the same OpenBB data path and `feature-engineering` functions as live
- Starts with `dummy_signals.py` (e.g. SMA crossover) to validate the engine before the real model
- Simulation only — no real orders during backtest

## Procedure

1. **Load historical candles** from the canonical Parquet dataset (see `src/data/dataset.py`; fetched via OpenBB for the `HISTORICAL_LOOKBACK` / `HISTORICAL_START_DATE`→`HISTORICAL_END_DATE` window at `HISTORICAL_BAR_SIZE`) into the canonical OHLCV schema.
2. **Walk forward bar by bar**: compute features → generate signal → apply risk checks → simulate fill (open/close) at the next bar's open plus slippage.
3. **Model costs**: apply spread/slippage and commission per trade — a backtest that ignores costs overstates edge.
4. **Split data**: train model on the earlier period, test on the later/unseen period (no lookahead).
5. **Compute metrics** exactly once: Sharpe (annualized), max drawdown, win rate, avg win/avg loss, worst week.
6. **Generate report** (equity curve, monthly returns, win/loss distribution) — the dashboard Backtest panel shows these from the last `src/backtest/engine.run_backtest` run.
7. **Compare to gate targets.** Fail → iterate model/features and re-run. Never proceed to live on a failing backtest.

## Gotchas
- Lookahead bias (using future bars) and survivorship are the top backtest killers — the shared feature pipeline is the safeguard.
- Same-features-in-both-modes invariant (see `feature-engineering`) is what makes backtest results transferable.
- **Market hours:** AAPL only trades ~9:30–16:00 ET on weekdays. Backtests must use the same trading calendar as live (skip non-trading days, handle overnight/weekend gaps) or results won't transfer. The scheduler should not expect live data outside market hours.

## References
- Project specs: Module 10 + gate criteria in `TRAIDER_PLAN.md`
