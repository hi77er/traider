---
name: risk-management
description: 'Configure and apply the risk settings that size a position and cap a run. Use when: working on src/risk/position_sizing.py, src/strategy/config.py (StrategyConfig), the Risk Management panel, or a setting that is empty and therefore NOT applied (STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT, RISK_LIMIT_PERCENT, MAX_EXPOSURE_PERCENT). Also read when touching the loss limits MAX_LOSS_PERCENT / MAX_CONSECUTIVE_LOSSES or src/strategy/limits.py, which the execution loop applies and a backtest does not.'
---

# Risk Management

The bot's first rule: **never risk more than you configured.** Two settings do the
work — an exposure cap and a stop distance — and every other risk knob is optional.

## When to Use
- Changing `src/risk/position_sizing.py` or `src/strategy/config.py`
- Adding or reordering a field in the Risk Management panel
- Working out why a backtest sized a position the way it did
- Reviewing `inputs.risk` in a stored run or the report's risk card
- Anything touching `MAX_CONSECUTIVE_LOSSES`, `MAX_LOSS_PERCENT` or
  `src/strategy/limits.py` — read "Where the loss limits apply" below first

## Project Facts

**EMPTY MEANS NOT APPLIED.** Every risk field is `Optional` and `None` means "leave
this behaviour out", in the backtest and in live/paper trading alike, because both
read the same values via `StrategyConfig.from_settings`. There is deliberately no
master switch: "apply the risk layer" and "circuit breaker enabled" were removed
because a switch can disagree with the settings it governs (armed, with every field
empty).

| Field | Default | Empty means |
|---|---|---|
| `MAX_EXPOSURE_PERCENT` | **100** | the whole account |
| `RISK_LIMIT_PERCENT` | empty | size by the exposure cap alone |
| `STOP_LOSS_PERCENT` | empty | no stop |
| `TAKE_PROFIT_PERCENT` | empty | no target |
| `MAX_LOSS_PERCENT` | empty | no daily halt (see below) |
| `MAX_CONSECUTIVE_LOSSES` | empty | no streak halt (see below) |
Zero and empty are different instructions: a 0% stop is a stop at the entry, and a
0% risk limit would size every position to nothing. That is why the blank string the
panel posts is turned into `None` by `_empty_risk_setting_to_none` rather than
coerced to a number.

**Sizing is ONE decision** — `StrategyConfig.deploy_weight_for(stop_pct)`, which
calls `risk.position_sizing.target_weight`:

- a risk limit AND a stop → risk-per-trade (`risk / stop`), trimmed by the exposure
  cap;
- otherwise → the exposure cap alone. A risk percentage with no stop has no distance
  to divide by, so falling back to the cap beats refusing to trade or silently
  risking nothing.

The volatility-target mode substitutes the realised volatility for the stop
distance, and the weight divides by whichever stop is really in place.

**The same config feeds three places**, which is the point: `StrategyEngine` (both
drivers), the backtest's `risk_sim` adapter (where `RiskConfig` is literally an alias
for `StrategyConfig`), and `signal_service` for the chart. A setting cannot apply in
one and not another without changing them all.

**Where the loss limits apply.** `MAX_LOSS_PERCENT` and `MAX_CONSECUTIVE_LOSSES` belong to the
**execution loop** — halting on losses belongs where the frequent decisions happen — and the policy
for them lives in `src/strategy/limits.py`. The tick supplies the day's trades and the account's
equity; a refused entry leaves the loop as a `SKIP`. Both limits are measured over one **exchange
day** and both clear at the day boundary, which is what makes a halt recoverable without an
operator: a halted bot takes no trades, so a streak that could only be broken by a win would never
be broken.

`Intent(action=SKIP)` in `src/strategy/engine.py` is the vocabulary for a refused entry, and the
ledger records one, so a halt is a leg in the ledger rather than a silence.

Consequences to know before touching them:

- **On a daily bar size both limits are inert.** One decision *is* the day, so the tally resets
  before a second decision can be evaluated against it. A daily-bar run is protected by its stops
  and its sizing. The Risk panel says so rather than offering a promise it cannot keep.
- **A backtest does not apply them.** It has no broker, so it has no day-start equity to measure
  `MAX_LOSS_PERCENT` against, and no per-tick halt to model. A backtest therefore shows entries a
  live loop would have refused — the one place the two deliberately differ beyond whole shares.
- **`MAX_LOSS_PERCENT` is EQUITY-based** (`day_pl = equity - last_equity`, from the account read),
  so it includes what is still open. `MAX_CONSECUTIVE_LOSSES` counts today's closed round trips
  whose `equity_ret < 0`; a skipped entry neither counts nor resets either one.
- **The retired modules are gone.** `src/risk/circuit_breaker.py` and `src/risk/validator.py` were
  deleted: the validator was a second, separately-stateful SIZING path, and the breaker kept its own
  counter where the ledger already records every leg. `AlpacaExecutor.validator` went with them.
  The breaker's own docstring is worth remembering — it documented that on daily bars it was inert
  either way, which is the fact the panel now reports.

## Procedure
1. **Read `StrategyConfig.from_settings`** before assuming a setting is in play: an
   absent value arrives as `None`, and `or 0.0` anywhere on that path is a bug.
2. **Size from the current equity** — live reads it from the broker
   (`AlpacaBroker.equity()`), the backtest applies the weight to each leg's return.
3. **Check the exposure cap against what is already committed** before adding another
   position (`size_position(..., existing_notional=)`).
4. **Log the decision** — the run's `inputs.risk` records what was in effect, and each
   trade row carries its exit reason, so a sizing question is answerable from a stored
   run.

## Gotchas
- **The repo's `.env` and a stray shell export both override a test's expectations.**
  `Settings(_env_file=None)` disables the dotenv file but NOT an exported environment
  variable, so a test about "no risk settings" must name every field explicitly
  (`_NO_RISK` in the test files) rather than rely on defaults.
- **Whole shares in live.** `AlpacaBroker.shares_for` floors, so a live position is
  never LARGER than the backtest modelled — the only place the two can differ.
- **A stop needs a level to rest at, and a bracket needs whole shares.** A fractional
  size with a stop is refused at the executor rather than sent unprotected.
- **`num()` on a null in the report JS** prints "—"; build sentences from the settings
  that are actually set, not from all of them.

## References
- Project specs: Module 5 in `TRAIDER_PLAN.md`
- `src/strategy/config.py` — the derived flags (`uses_stop`, `sizes_by_risk`,
  `deploy_weight`, `applied`)
- `src/web/services/config_service.py` — `_STRATEGY_SCOPE` "Risk Management" (panel
  order) and `_HINTS` (the example line under each field)
