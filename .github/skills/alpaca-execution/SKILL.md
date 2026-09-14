---
name: alpaca-execution
description: 'Place and manage orders through the Alpaca Trading API (paper or live). Use when: building src/execution/alpaca_executor.py, resolving which environment an order goes to, wiring stop-loss/take-profit as bracket or OCO orders, polling order status, retry with exponential backoff, testing execution with mocks, or debugging refused orders. Alpaca is execution-only in this project — data comes from OpenBB.'
---

# Alpaca Execution (Order Placement)

TRAIDER places orders through the **Alpaca Trading API**. Alpaca is
**execution-only**: every candle, quote and indicator comes from OpenBB.

Alpaca was chosen over the IBKR Client Portal Gateway because it authenticates
with an **API key pair** — no browser, no 2FA prompt, no session that expires
daily. That is the difference between "runs unattended in the cloud" and "needs
a human with a phone every morning".

## When to Use
- Implementing `src/execution/alpaca_executor.py` and `retry.py`
- Resolving which environment (paper/live) an order belongs to
- Attaching a stop-loss and take-profit to an entry (bracket / OCO)
- Polling order status, handling partial fills, cancelling
- Mocking Alpaca in tests so CI never contacts a real account
- Debugging a refused order or a refused *configuration*

## Project Facts
- **Resolution lives in `src/execution/config.py`** and nowhere else. It returns
  an `ExecutionTarget` (broker, env, base_url, key_id, secret, live) or raises
  `ExecutionConfigError`. Never re-derive the environment anywhere else.
- Config split — **account-wide** (`settings/account/account.json`):
  `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`,
  `ALPACA_LIVE_API_KEY`, `ALPACA_LIVE_API_SECRET`.
  **Per strategy** (strategy panel, "Execution"): `EXECUTION_ENV` (`paper` |
  `live`), `EXECUTION_LIVE_ACK`. There is no broker selector — Alpaca is the
  only broker (`BROKER` in `src/execution/config.py`).
- `EXECUTION_MAX_RETRIES`, `EXECUTION_RETRY_BASE_DELAY_SECONDS`,
  `EXECUTION_ORDER_TIMEOUT_SECONDS` drive retry/backoff/timeout.
- Only the execution module may talk to a broker. The risk layer must approve
  every order first (`src/risk/validator.py`) — in BOTH environments, with the
  same limits.

## The switch (paper vs live)
Paper and live are the **same API** with a different base URL and key pair:

| | Paper | Live |
|---|---|---|
| Base URL | `https://paper-api.alpaca.markets` | `https://api.alpaca.markets` |
| Keys | `ALPACA_PAPER_*` | `ALPACA_LIVE_*` |
| Money | simulated | real |

**LIVE requires two independent keys**: `EXECUTION_ENV=live` **and**
`EXECUTION_LIVE_ACK=true`, **and** a live key pair. Any of the three missing is
refused — never downgraded to paper. A misconfiguration must stop trading, not
quietly happen somewhere else.

Key any cached broker state (positions, open orders) by
**(environment, account)**, not by symbol. Caching by symbol alone means
switching environments shows the other account's positions and the strategy
acts on stale state.

## Procedure
1. **Resolve** the target: `resolve_execution_target(settings)`. Let
   `ExecutionConfigError` propagate as a hard stop with a clear alert.
2. **Authenticate** with HTTP Basic on every request — `APCA-API-KEY-ID` and
   `APCA-API-SECRET-KEY` headers. There is no login call and no token refresh.
3. **Build the order** in `place_order(instrument, side, quantity,
   stop_loss_price, take_profit_price)`; validate side, positive quantity and
   sane prices before sending.
4. **Attach the exits with ONE call.** Alpaca has broker-side **bracket**
   (entry + take-profit + stop-loss, one-cancels-other), **OCO** (add both exits
   after a position is open) and **OTO**. Use them — handwritten OCO is the
   classic bug: a filled take-profit leaves a live stop order that opens the
   opposite position. `POST /v2/orders` with `order_class: "bracket"`.
5. **Submit** and capture the order id; **poll** until filled / `order_timeout`,
   treating partial fills explicitly.
6. **Wrap in `execute_with_retry`**: exponential backoff, log every attempt,
   alert on final failure, update state on success/failure.
7. **Log the environment on every order line** (PAPER/LIVE). Both accounts look
   identical in the portal, so an unlabelled log is how live orders get missed.

## Gotchas
- **Fractional orders are DAY-only** — no GTC. GTC brackets/OCO need whole-share
  quantities, and position sizing can produce fractional sizes, so decide which
  you want before wiring brackets.
- **A stop price must sit at least $0.01 beyond the base price** (the entry
  limit, or the current market price), or the request is rejected.
- **Buying power** applies to short sells too, at `max(limit, 3% above ask) × qty`.
- Paper does **not** simulate slippage, fees, dividends, queue position or price
  improvement, so paper P&L beats both the backtest and live. Never treat a
  paper-vs-backtest gap as a bug.
- Errors surface as `{"code": ..., "message": ...}` with a per-request
  `X-Request-ID` header — log both, they are what support asks for.
- Tests must mock the HTTP layer (`responses` / monkeypatch) so CI never
  contacts a real account, and must never use a real key.

## References
- Trading API docs: https://docs.alpaca.markets/us/docs/trading-api
- Orders + bracket/OCO/OTO: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Paper trading: https://docs.alpaca.markets/us/docs/paper-trading
- Project specs: Module 6 in `TRAIDER_PLAN.md`
