---
name: feature-engineering
description: 'Compute technical indicator features for AAPL (Apple) stock from OHLCV candles. Use when: implementing src/features/engineering.py with SMA/EMA/MACD/RSI/ATR/Bollinger/momentum/volatility, choosing lookback windows and parameters, or guaranteeing feature code is identical between backtest and live to avoid lookahead bias.'
---

# Feature Engineering

Transforms raw OpenBB OHLCV candles into the numeric feature vector consumed by the signal model. The single most important rule: **identical feature computation in backtest and live** — a mismatch invalidates every backtest result.

## When to Use
- Implementing `src/features/engineering.py`
- Adding/validating indicators (SMA, EMA, MACD, RSI, ATR, Bollinger %B, momentum, rolling volatility, VWAP, relative volume, absolute volume)
- Choosing lookback windows and indicator parameters
- Verifying backtest/live feature consistency (`feature-validation` task)
- Debugging NaN/inf handling at series boundaries

## Project Facts
- **All indicator parameters are configured via `.env` — never hardcode them.** Windows/periods come from `FEATURES_SMA_PERIODS`, `FEATURES_EMA_PERIODS`, `FEATURES_MACD_FAST_PERIOD`, `FEATURES_MACD_SLOW_PERIOD`, `FEATURES_MACD_SIGNAL_PERIOD`, `FEATURES_RSI_PERIOD`, `FEATURES_ATR_PERIOD`, `FEATURES_BOLLINGER_PERIOD`, `FEATURES_BOLLINGER_STD`, `FEATURES_MOMENTUM_PERIODS`, `FEATURES_VOLATILITY_PERIOD`, `FEATURES_VWAP_PERIOD`, `FEATURES_VOLUME_PERIOD`, `FEATURES_MIN_LOOKBACK`
- **Indicators can be switched on/off independently** via `FEATURE_SMA_ENABLED`, `FEATURE_EMA_ENABLED`, `FEATURE_MACD_ENABLED`, `FEATURE_RSI_ENABLED`, `FEATURE_ATR_ENABLED`, `FEATURE_BOLLINGER_ENABLED`, `FEATURE_MOMENTUM_ENABLED`, `FEATURE_VOLATILITY_ENABLED`, `FEATURE_VWAP_ENABLED`, `FEATURE_VOLUME_ENABLED`, `FEATURE_VOLUME_ABS_ENABLED` (values on/off/True/False)
- Input: canonical OHLCV DataFrame from `openbb_client.py` (same schema for both modes)
- Output: one row of features per decision point; no leakage of future bars

## Procedure

1. **Compute on rolling windows only** — each feature at bar `t` uses data up to `t` exclusively.
2. **Align features to the decision cadence** — there is no cadence setting: a decision is made on every newly generated bar, so the features are simply computed per bar of `HISTORICAL_BAR_SIZE` and backtest and live produce the same feature vector for the same timestamp.
3. **Handle warmup**: require a minimum lookback (e.g. max window) before emitting features; drop or NaN the warmup period consistently in both modes.
4. **Write the shared compute in one function** used by both the backtester and the live orchestrator — never two copies.
5. **Validate with fixtures**: known candles → expected feature values; compare backtest vs live vectors on the same data (`feature-validation`).

## Gotchas
- pandas rolling functions shift/shift pitfalls cause lookahead — add tests that assert no feature uses `close[t+1]`.
- Inconsistent NaN handling between modes silently corrupts the model.

## References
- Project specs: Module 3 in `TRAIDER_PLAN.md`
