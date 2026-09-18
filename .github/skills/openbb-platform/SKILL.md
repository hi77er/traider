---
name: openbb-platform
description: 'Work with the OpenBB Platform SDK (openbb package) to fetch market data for AAPL (Apple) stock. Use when: pulling historical OHLCV candles, live quotes, or intraday data; the single yfinance provider and what to do when it rate-limits; handling the standardized OpenBB output schema; running the platform as a REST server; mocking OpenBB in tests; or ensuring identical data flow between backtest and live.'
---

# OpenBB Platform (Data Layer)

The TRAIDER bot gets ALL price data from the **OpenBB Platform SDK** (`from openbb import obb`). OpenBB aggregates many providers behind a standardized output schema. It is **data-only** — it cannot place orders (that is the `alpaca-execution` skill).

## When to Use
- Implementing `src/data/openbb_client.py`, `historical.py`, `live.py`
- Choosing/validating a provider for AAPL (`INSTRUMENT` from config)
- Fetching historical candles for backtesting/training
- Reacting to the latest completed bar during market hours (a decision per newly generated bar; no polling cadence to configure)
- Mocking OpenBB responses in unit tests
- Troubleshooting "data mismatch between backtest and live"

## Project Facts
- Provider: **yfinance only** — the constant `config.history.DATA_PROVIDER`. There is no `OPENBB_PROVIDER` / `OPENBB_API_KEY` setting and no fallback chain: the premium providers were removed, and the client raises the provider's own error text instead of aggregating backups
- Historical window & bar size from config: `HISTORICAL_BAR_SIZE` (1m/2m/5m/15m/1h/2h/4h/8h/12h/1d) and `HISTORICAL_LOOKBACK` (`"2y"`/`"30d"`, counted back from now; `HISTORICAL_START_DATE`/`HISTORICAL_END_DATE` are the fallback when it is empty); instrument from `INSTRUMENT`
- **Canonical dataset:** `fetch_candles` persists the backfill to Parquet in `HISTORICAL_DATA_DIR` (e.g. `data/historical/AAPL_1d.parquet`) — backtesting/training read this stable store via `load_dataset`. The `.cache/` CSVs are only the live-poll fast-path cache
- **Optional S3 sync:** when `S3_ENABLED=True` + `S3_BUCKET` is set, the dataset is written locally then uploaded to S3 (durable source of truth); a missing local dataset is auto-restored from S3. Enable bucket versioning for crash safety
- Symlink-free invariant: **the exact same OpenBB code path must run in backtest and live** — wrap all calls in `openbb_client.py`
- Output: OpenBB returns standardized results; call `.to_df()` for a pandas DataFrame of OHLCV
- **Interval resampling:** yfinance only supports 1m–1Q intervals (no `4h`). `OpenBBClient` auto-fetches `1h` and resamples to the configured `HISTORICAL_BAR_SIZE` when it isn't natively supported (2h/4h/8h/12h). The default `HISTORICAL_BAR_SIZE=1d` needs no resampling.
- **Intraday depth is a PROVIDER limit, not a setting:** yfinance serves ~7 days of 1m bars and ~60 days of 2m/5m/15m/30m/60m bars, ~730 days of hourly, everything for daily. That is why `src/config/history.py` binds each bar size to the periods it may be fetched for and the dashboard only offers those. Note the notation collides: `1m` is one MINUTE, `1M` is one MONTH.

## Procedure

1. **Use the plain ticker.** AAPL is a listed equity, so `INSTRUMENT=AAPL` works directly across providers (yfinance, polygon, fmp, tradier) — no futures/forex symbol mapping needed. Keep the ticker in config, not hardcoded.
2. **Wrap OpenBB behind `openbb_client.py`** so callers never import `obb` directly:
   ```python
   from openbb import obb

   def fetch_historical(symbol, start, end, interval, provider):
       data = obb.equity.price.historical(
           symbol, start_date=start, end_date=end,
           interval=interval, provider=provider,
       ).to_df()
       # normalize to a canonical OHLCV DataFrame (rename/type columns)
       return normalize_ohlcv(data)
   ```
3. **Normalize output** into one canonical schema (columns: `open, high, low, close, volume, ts`). Both `historical.py` and `live.py` consume this schema.
4. **Cache** historical data locally (parquet/csv) and the live candle for the interval duration to respect rate limits.
5. **Provider failover:** if the primary provider raises, try a backup provider before failing the run.
6. **Tests:** monkeypatch `obb` in `test_data.py` — never hit a real provider in CI.

## Gotchas
- Free providers (`yfinance`) can throttle or have delayed data — never assume real-time accuracy for execution decisions; use for signals only.
- **Yahoo rate-limits aggressively** (observed `YFRateLimitError: Too Many Requests`), especially plain `requests` sessions with no browser TLS fingerprint. Fix: `src/data/openbb_session.py` patches OpenBB to pass a `curl_cffi` session (`impersonate="chrome"`) to yfinance — applied automatically by `OpenBBClient`. If throttling still occurs, wait and retry: there is no fallback provider by design.
- `openbb` may upgrade `numpy`/`pandas` on install — re-freeze `requirements.txt` after `pip install openbb`.
- yfinance is at `0.2.66` (latest 0.2.x) — keep it in the `>=0.2.55,<0.3.0` range openbb-yfinance 1.4.2 requires, and keep curl_cffi installed.
- The provider is a constant (`history.DATA_PROVIDER`), never a configured choice; never hardcode symbols or keys.

## References
- Docs: https://docs.openbb.co/platform
- Project specs: see Module 2 in `TRAIDER_PLAN.md` and Appendix B
