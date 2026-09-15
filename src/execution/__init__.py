"""Execution: the only part of TRAIDER that can move money.

Layered, outermost last — each layer knows only about the one below it, so an order
can be reasoned about at the level that matters for the question being asked:

| Module | The question it answers |
|---|---|
| ``config`` | *Where* would an order go — paper or live? Resolved once, raises rather than downgrading |
| ``credentials`` | Are the keys **working**, or merely present? Answered by asking Alpaca, not by inspecting the file |
| ``alpaca_client`` | What is the API, in URLs and status codes? Auth headers, timeouts, what is retryable |
| ``retry`` | May this call be tried again? Only a failure that never reached a verdict |
| ``alpaca_executor`` | Place ONE order: refuse before sending, bracket the exits, poll, report |
| ``alpaca_broker`` | The strategy's ``Broker`` seam: turn an ``Intent`` into that order and report the fill |

Two rules hold the whole thing together.

**Nothing here decides anything.** Sizing, stops, the circuit breaker and the
decision to trade all live in ``src/risk`` and ``src/strategy``, where the backtest
uses them too. This package receives numbers and sends them.

**Nothing outside this package may talk to a broker.** Every other module asks
``alpaca_broker`` or ``alpaca_executor``; the credential check is the single
exception, because proving a key works means asking the key's owner.

The environment is resolved in exactly one place, so there is never a second opinion
about whether real money is at stake — and the trading switch is checked here, in the
last gate before an order exists.
"""

from src.execution.alpaca_broker import AlpacaBroker, broker_position, shares_for
from src.execution.alpaca_client import AlpacaClient, AlpacaError
from src.execution.alpaca_executor import AlpacaExecutor, OrderRefused, build_order_payload
from src.execution.config import (
    BROKER,
    ENVIRONMENTS,
    ExecutionConfigError,
    ExecutionTarget,
    execution_status,
    resolve_execution_target,
)
from src.execution.retry import execute_with_retry

__all__ = [
    # configuration — where an order goes
    "BROKER",
    "ENVIRONMENTS",
    "ExecutionConfigError",
    "ExecutionTarget",
    "execution_status",
    "resolve_execution_target",
    # placing an order
    "AlpacaClient",
    "AlpacaError",
    "AlpacaExecutor",
    "OrderRefused",
    "build_order_payload",
    "execute_with_retry",
    # the strategy's broker seam
    "AlpacaBroker",
    "broker_position",
    "shares_for",
]
