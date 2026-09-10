---
name: risk-management
description: 'Apply the risk layer that vetoes risky signals. Use when: implementing src/risk/position_sizing.py (1-2% risk, volatility targeting), circuit_breaker.py (max consecutive losses, daily drawdown limit), validator.py (unified approve/reject), or enforcing MAX_LOSS_PERCENT / MAX_CONSECUTIVE_LOSSES. Guarantees the bot never loses more than configured.'
---

# Risk Management

The bot's first rule: **never lose more than you allow.** Every signal passes through the risk layer before the IBKR executor may act. Risk vetoes are logged so you can always see *why* a trade was rejected.

## When to Use
- Implementing `src/risk/position_sizing.py`, `circuit_breaker.py`, `validator.py`
- Computing position size from account equity, risk limit, and stop distance
- Tracking consecutive losses / daily drawdown to halt trading
- Reviewing vetoed signals in logs
- Enforcing config limits (`RISK_LIMIT_PERCENT`, `MAX_LOSS_PERCENT`, `MAX_CONSECUTIVE_LOSSES`)

## Project Facts
- **Every limit/mode is configured via `.env`** — `RISK_LIMIT_PERCENT`, `MAX_LOSS_PERCENT`, `MAX_CONSECUTIVE_LOSSES`, `MAX_EXPOSURE_PERCENT`, `POSITION_SIZING_MODE` (`fixed_risk`|`volatility_target`), `STOP_LOSS_PERCENT`, `TAKE_PROFIT_PERCENT`, `CIRCUIT_BREAKER_ENABLED`
- Position sizing formula: `position_size = (account_size × RISK_LIMIT_PERCENT) / stop_loss_distance`
- Circuit breaker: stop trading for the day after `MAX_CONSECUTIVE_LOSSES` or daily drawdown > `MAX_LOSS_PERCENT`; reset next day/week
- `RiskValidator.validate_signal(signal, state)` returns `(approved: bool, reason: str)` — single entry point, logs every veto
- Execution (IBKR) must never be called without risk approval

## Procedure

1. **Size the position** from current account equity, the risk limit, and the stop-loss distance; cap at max exposure, accounting for open positions.
2. **Check the circuit breaker**: consecutive losses and current drawdown vs limits; if tripped, reject all signals until reset.
3. **Validate the full signal** through the single `RiskValidator` (size ≤ exposure, breaker inactive, stop distance sane, instrument allowed).
4. **Log every outcome** — approve with size, or veto with reason — so decisions are auditable.
5. **Update breaker state** after each filled trade (win/loss) via the state tracker.

## Gotchas
- Position sizing must use the *current* equity from the state tracker, never a stale value.
- Circuit breaker state must survive restarts (persist in DynamoDB) or a crash could reset loss limits.

## References
- Project specs: Module 5 in `TRAIDER_PLAN.md`
