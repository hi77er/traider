"""The strategy pipeline: one implementation, two drivers.

Everything that decides what the bot DOES lives here, and nothing that decides it
lives anywhere else:

* :mod:`src.strategy.engine` — the bar-at-a-time state machine (``StrategyEngine.step``)
  and the accounting that turns fills into trades and returns;
* :mod:`src.strategy.state` — the strategy's state (position, last decided bar) and its
  serialization, so a live run can be restarted without re-deciding;
* :mod:`src.strategy.config` — the knobs the machine reads, resolved from the
  strategy's effective settings;
* :mod:`src.strategy.broker` — the small seam between a decision and a fill;
* :mod:`src.strategy.limits` — the day's loss limits. Not a trading rule and not in a
  driver: the execution LOOP applies them, so a backtest does not. That is the one place a
  backtest and a live run deliberately differ, and it is why they live in their own module
  rather than in an ``if`` inside a driver.

The point of the split is the guarantee the backtest needs to be worth anything: the
backtest and a live run feed the SAME machine, one bar at a time, and differ only in
where the bars come from and who fills the order. Neither driver is allowed to contain
a trading rule — if one ever grows an ``if`` about stops or sizing, the guarantee is
already broken, and ``tests/test_strategy_parity.py`` fails.
"""

from __future__ import annotations

from src.strategy.config import StrategyConfig
from src.strategy.engine import (
    CLOSE,
    NONE,
    OPEN,
    SKIP,
    Intent,
    Ledger,
    StrategyEngine,
    bar_from_row,
)
from src.strategy.state import Position, StrategyState

__all__ = [
    "CLOSE",
    "NONE",
    "OPEN",
    "SKIP",
    "Intent",
    "Ledger",
    "Position",
    "StrategyConfig",
    "StrategyEngine",
    "StrategyState",
    "bar_from_row",
]
