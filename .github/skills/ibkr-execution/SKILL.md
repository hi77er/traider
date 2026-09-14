---
name: ibkr-execution
description: 'Place and manage AAPL (Apple) stock orders via the Interactive Brokers Web API (Client Portal Gateway). Use when: building src/execution/ibkr_executor.py, constructing order objects, IBKR session authentication, polling order status, retry logic with exponential backoff, testing execution with mocks, or debugging rejected/failed orders. IBKR is execution-only in this project — data comes from OpenBB.'
---

# IBKR Execution (Order Placement)

The TRAIDER bot places orders **only** through the Interactive Brokers Web API via the Client Portal Gateway (Java process in the Docker image). IBKR is **execution-only** — all price data comes from OpenBB (`openbb-platform` skill).

## When to Use
- Implementing `src/execution/ibkr_executor.py` and `src/execution/retry.py`
- Building/validating order objects (side, quantity, stop-loss, take-profit)
- Handling IBKR auth (login/session token) and re-auth
- Polling order status until filled or timeout
- Adding retry with exponential backoff (1s, 2s, 4s)
- Mocking IBKR in unit/integration tests
- Debugging order rejections

## Project Facts
- **STATUS: there is no IBKR executor.** `EXECUTION_BROKER=ibkr` is accepted by
  the config but refused by `src/execution/config.py`, because the implemented
  path is Alpaca — see the `alpaca-execution` skill. Everything below describes
  what IBKR execution *would* require.
- Config: `IBKR_API_URL`, `IBKR_ACCOUNT_ID`, `IBKR_USERNAME`, `IBKR_PASSWORD` (execution only).
  The paper/live switch is `EXECUTION_ENV` (per strategy) plus `EXECUTION_LIVE_ACK` — never
  an IBKR-specific flag.
- Retry/backoff/timeout from config: `EXECUTION_MAX_RETRIES`, `EXECUTION_RETRY_BASE_DELAY_SECONDS`, `EXECUTION_ORDER_TIMEOUT_SECONDS`
- Gateway runs on port 5000 in the Docker container; entrypoint starts gateway + bot
- Only the execution module may touch IBKR — nothing else should talk to it
- Risk module must approve every order before submission

## Procedure

1. **Authenticate** — the gateway has NO credential endpoint. You log into
   `https://localhost:5000` **once, interactively in a browser** (IBKR login +
   2FA) and the gateway holds the session; the bot merely talks to that
   already-authenticated local gateway. There is no `POST /auth`, no session
   token to send, and therefore nothing for the app to store — which is why a
   stored password does not unlock unattended trading. Verify with
   `GET /v1/api/iserver/auth/status`, keep the session alive via `POST /v1/api/tickle`,
   and treat "not authenticated" as a hard stop that alerts a human.
2. **Build the order** in `IBKRExecutor.place_order(instrument, side, quantity, stop_loss_price, take_profit_price)` — validate fields (side, positive quantity, sane prices) before sending.
3. **Submit** via the Web API and capture the `order_id`.
4. **Poll** order status until `Filled` or a timeout; treat partial fills explicitly.
5. **Wrap in `execute_with_retry`** (`retry.py`): exponential backoff, log every attempt, alert on final failure, update state tracker on success/failure.
6. **Tests:** mock the HTTP layer (`responses`/monkeypatch) so CI never contacts the gateway.

## Gotchas
- Gateway requires Java — keep the Java runtime in the Docker image even though data moved to OpenBB.
- Orders can be rejected for bad format, insufficient margin, or session expiry — log the raw rejection.
- Paper trading must use an IBKR paper account, never real money.

## References
- IBKR Web API: https://www.interactivebrokers.com/en/trading/web-api
- Gateway: https://github.com/InteractiveBrokers/cpapi-web-gateway
- Project specs: Module 6 in `TRAIDER_PLAN.md`
