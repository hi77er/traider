"""One-shot backfill: fetch the configured historical window and persist the
canonical Parquet dataset (``data/historical/{symbol}_{interval}.parquet``).

Usage:
    python -m scripts.backfill                  # uses .env config (AAPL 1d)
    python -m scripts.backfill --symbol MSFT --bar-size 1d --start 2021-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the project root is importable when run as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import get_settings  # noqa: E402
from src.data.dataset import dataset_path, load_dataset  # noqa: E402
from src.data.historical import fetch_candles  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch initial historical data")
    parser.add_argument("--symbol", default=None, help="Override INSTRUMENT")
    parser.add_argument("--bar-size", default=None, help="Override HISTORICAL_BAR_SIZE")
    parser.add_argument("--start", default=None, help="Override HISTORICAL_START_DATE")
    parser.add_argument("--end", default=None, help="Override HISTORICAL_END_DATE")
    args = parser.parse_args()

    settings = get_settings()
    symbol = args.symbol or settings.instrument
    bar_size = args.bar_size or settings.historical_bar_size
    start = args.start or settings.historical_start_date
    end = args.end or settings.historical_end_date

    print(f"Backfilling {symbol} {bar_size} bars from {start} -> {end or 'now'}...")
    try:
        df = fetch_candles(
            settings,
            symbol=symbol,
            start_date=start,
            end_date=end,
            bar_size=bar_size,
        )
    except Exception as exc:
        print(f"\nBackfill failed: {exc}")
        print(
            "\nTip: the yfinance provider is free but rate-limits aggressively "
            "(YFRateLimitError). Wait a bit and retry, or set OPENBB_PROVIDER + a "
            "free API key (FMP/Tiingo) in .env for reliability."
        )
        return 1

    path = dataset_path(settings, symbol, bar_size)
    total = len(load_dataset(settings, symbol, bar_size))
    print(f"\nBackfill complete: {len(df)} rows fetched -> {path}")
    print(f"Canonical dataset now holds {total} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
