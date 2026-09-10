"""Backtesting for TRAIDER (pure library — no web/HTTP imports).

The backtest engine replays the ACTIVE strategy's rule-based model over the
canonical Parquet dataset, in the exact context (features, thresholds,
instrument, bar size) used live, so results transfer to real trading.

Public API (see the modules):
- ``engine.run_backtest(settings, dataset=None)``  -> result dict
- ``metrics.compute_metrics(...)`` / ``metrics.gate_result(...)``
"""

from __future__ import annotations

from . import engine, metrics

__all__ = ["engine", "metrics"]
