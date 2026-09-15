# TRAIDER

A single-instrument, rule-based trading bot that is developed, backtested and
operated from one place: a FastAPI web portal on top of a pure-Python strategy
stack.

The central design rule is that **the backtest replays the same code the live
bot would run**. Data fetching, feature computation, signal generation,
position sizing, stop/take handling and the circuit breaker all live in
importable modules, and the backtest drives them directly - so a number in the
report describes the system that would actually trade, not a simplified
replay of it.

## Status

| Area | State |
| --- | --- |
| Data pipeline (OpenBB + yfinance, Parquet store, delta backfill) | done |
| Features, rule model, risk layer, backtest engine + Gate | done |
| Web portal (chart, config, backtest panel, report page) | done |
| Tests | 418 passing |
| Execution config — Alpaca broker, per-strategy paper/live, fail-closed | done |
| Trading on/off switch + the "no reconfiguration while trading is on" lock | done |
| Live order execution (the Alpaca executor itself) | **not implemented** |
| Portfolio state (DynamoDB) | **not implemented** |
| Scheduler / bot entry point | **not implemented** - `src/main.py` is a stub |

The current strategy **does not pass its own Gate yet** (see
[CHECKLIST.md](CHECKLIST.md) for the metrics). Treat every stored result as
research, not as an expectation.

## How it works

```
OpenBB/yfinance ──> canonical Parquet dataset ──> features ──> rule model
                        (data/historical)             │            │
                                                      └──────> signals
                                                                  │
                        ┌─────────────────────────────────────────┘
                        v
                 RISK LAYER  (sizing -> stop / take -> circuit breaker)
                        │
          ┌─────────────┴──────────────┐
          v                            v
   BACKTEST (next-open fills)     EXECUTION (Alpaca paper / live)
          │                            │   ^ config + paper/live switch
          │                            │     + trading on/off lock done,
          │                            │     executor not implemented
          v                            v
   report page + run store        portfolio state <- not implemented
```

Two consequences worth knowing:

- **A signal is not a trade.** The risk layer decides whether an entry is
  taken at all (the circuit breaker can veto it), how big it is, and when the
  position actually ends (stop, take profit, or the opposite signal). The
  chart, the backtest panel and the report all read the risk layer's output,
  so they cannot disagree.
- **`APPLY_RISK_LAYER` toggles the entire layer** for a strategy. With it off
  the backtest replays the raw strategy - no sizing, no stops, no halt. That
  mode is useful for attribution ("does my edge survive a stop?") but its
  numbers are not achievable live.

## Requirements

- Python 3.9 (developed and tested on 3.9.6)
- No API key needed for the default setup - the data layer uses yfinance
- Alpaca **paper** API keys only if you want the bot to place orders - paper
  keys are free and available worldwide, and are not required to backtest or to
  use the portal

## Setup

```bash
python3.9 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env        # the defaults work as-is for a local, data-only run
```

`.env` holds ~50 settings; `.env.example` documents every one of them with
comments. Real values never leave your machine - `.env` is gitignored, and the
template ships with every credential blank.

## Run the portal

```bash
.venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000
# -> http://127.0.0.1:8000
```

Use `.venv/bin/python` explicitly; a bare `python` may resolve to a different
interpreter.

The portal is the whole interface:

- **Strategy bar** - switch, create, rename or delete a strategy (each one has
  its own instrument, bar size, rules and risk settings)
- **Trading switch** (header, left, beside the account) - the master ON/OFF for the
  active strategy, and the only place trading is started. Starting it always asks
  first, on paper as well as live: starting the bot is a deliberate act either way,
  and a confirmation that only appears sometimes is one you stop reading. The
  wording is what differs - on the live account it is about real money, on paper it
  is about the strategy acting on the next signal. Stopping never asks, so it is
  always one click.
- **Environment dropdown** (header, left, beside the logo) - which Alpaca account the
  active strategy's orders go to. It both selects and displays the mode, and the
  labels spell out the consequence rather than tinting the control: `Paper —
  simulated, no real money` / `LIVE — REAL ORDERS`. When the selected account has no
  keys, so orders would be refused, the label says so (`— ⚠ no keys`) and the
  tooltip carries the reason. Paper vs live is per strategy, so one strategy can run
  on the live account while another stays on paper. Only a real gesture on the
  dropdown can change it: a browser-restored value (bfcache, back/forward, crash
  recovery) fires `change` without anyone choosing anything, so it is ignored and
  the display snaps back to what the server said.
- **Trading panel** (appears under the chart while trading is ON) - a standing
  reminder that the strategy is armed, with a one-click stop. While it is
  visible every configuration surface is locked and the backtest buttons are
  disabled: nothing that would change what the bot is running may be edited
  mid-flight.
- **Price chart** - candles, indicators, BUY/SELL markers, the held-period
  bands (green when the round trip made money, red when it lost) and the
  stop/take exits
- **Strategy Configuration / Rules / Risk Management** - three collapsible
  panels; the risk panel leads with the two master switches
- **Account Settings** (🏦 header popup) - the Alpaca key pairs, the single data
  folder, backtest defaults and cloud/state storage; shared by all strategies. Each
  key pair has a **Validate** button, which checks **only what the boxes contain**
  (untyped-but-unsaved included) - an empty form is answered with "no credentials
  found to validate" rather than quietly checking the stored pair, so the reply is
  always about a key that is on screen. Saving is the other way round: a blank or
  masked field there means "unchanged". The badge is the
  **result of a check, not stored state**: the rows are clean when the popup opens,
  and a pair is labelled only once it has been checked in that visit - by Validate
  (`✓ verified · <account> · <time>`, or `⚠ not valid` with the reason on hover) or
  by the check a save runs on a newly added pair. A stored verdict is not painted on
  open, because a masked box with "⚠ not valid" beside it reads as a bug rather than
  as news. A pair that already passed is not re-checked on every save. A bad pair
  fails itself rather than the save: a pair the broker **rejects** is left out of the
  write (see Safety) while the rest of the form is saved as normal.
- **Global Settings** (⚙ header popup) - the data provider + keys in `.env`
- **Backtest panel** - run the engine, read the Gate and the metrics
- **Historical Delta** - gap-check the dataset against the provider and refill
  missing bars
- **Report page** - the full picture for any stored run: equity vs buy & hold,
  drawdown, monthly/yearly returns, the trade distribution, the exit-reason
  breakdown and the entries the circuit breaker refused
- **Market page** (`/market`, 🌎 header button) - whole-market screening from
  Yahoo Finance: a preset screener, the whole US market, top gainers, highest
  volume, top losers and the small-cap gainers/volume lists. The two long tables
  are collapsible and start collapsed so the page opens as an overview

The two header controls are **one pill in every state** - same border, radius,
padding, height, font, tint, background and text colour, whichever account is
selected and whether or not trading is on. Nothing is styled per state, and the
class list is constant, so the pair cannot drift apart; the state is carried by the
words (`LIVE — REAL ORDERS` next to `▶ Turn trading on`) rather than by a palette
you have to remember. The dropdown is forced into the button's box for this: no
caret, the same 34px height, and its label centred like the button's.

Each label ends with a **status dot**: blue for the calm state (paper account,
trading off), and a red one that blinks for the state that spends money or is live
(live account, trading on). Both dots are driven by one clock, so when both are red
they blink in step. A dot is part of the TEXT rather than a styled element: the
account control is a native `<select>`, whose options can only hold text and which
always sizes itself to its widest option, so a positioned element could never sit
at the end of the selected label. Under `prefers-reduced-motion: reduce` the alert
dot stays red and simply stops flashing.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 418 passed
```

The suite is offline: OpenBB, the broker and the clock are all stubbed, so it
runs without credentials or market data. The dashboard's and report page's
crosshair-sync logic is exercised by running the real `app.js` / `report.js`
blocks under `node` against fake charts that reproduce lightweight-charts'
actual event semantics (see `tests/test_web/test_crosshair_sync.py`), so it needs
`node` on `PATH`; those tests skip themselves when it is absent.

## Project layout

```
src/
  config/      settings (pydantic) + per-strategy effective settings
  data/        OpenBB client, provider session, screener (Yahoo), dataset, delta backfill
  features/    indicators + feature engineering (no lookahead)
  model/       rule store and rule-based signal generator
  risk/        position sizing, stop/take levels, circuit breaker, validator
  backtest/    engine, metrics + Gate, risk replay, report analytics, run store
  execution/   Alpaca paper/live resolver (config); executor not implemented
  state/       portfolio tracker      (not implemented)
  scheduler/   decision loop          (not implemented)
  logging/     structured logging, alerts (not implemented)
  web/         FastAPI app, routes, services (incl. the trading lock), templates, static assets
tests/         pytest suite (offline)
settings/      LOCAL DATA (gitignored): strategies/store.json + account/account.json
data/          generated at runtime: historical Parquet + backtest results
```

Runtime data is not in the repository:

```
data/historical/<SYMBOL>_<bar>.parquet         canonical OHLCV
data/backtest_results/<strategy>/latest.json   trimmed view the panel reads
data/backtest_results/<strategy>/runs/<id>.json full, self-describing run
data/backtest_results/<strategy>/index.json    run-menu index
data/trading.json                              trading ON/OFF (runtime, not config)
```

The trading state is deliberately **not** configuration: it lives beside the
datasets because the configuration files it freezes cannot hold the switch that
freezes them.

A run file is deliberately complete: every equity point, every trade with its
exit reason, and an `inputs` block (settings, rules + fingerprint, window,
costs, risk config) that makes a result reproducible and attributable.

## Configuration

Three layers, each with its own editor in the dashboard:

| Layer | File | Edited from | Holds |
|-------|------|-------------|-------|
| **Global** | `.env` | ⚙ Global Settings | data provider + keys, the paths of the two JSON stores |
| **Account** | `settings/account/account.json` | 🏦 Account Settings | the Alpaca key pairs (paper + live), the data folder, backtest defaults, cloud/state storage |
| **Strategy** | `settings/strategies/store.json` | Strategy Configuration / Rules / Risk panels, plus the header dropdown for `EXECUTION_ENV` | instrument, bar size, features, model, gates, schedule, risk limits, rules, paper/live |

Precedence is **strategy > account > .env**, and the process environment still
wins over `.env` (which is why a stray exported variable can silently override
it). Both JSON files are LOCAL DATA: gitignored, and created on first save.

One key is stored per strategy but edited outside the panels: `EXECUTION_ENV`
comes from the header dropdown. Saving a panel preserves it (the panel never
renders it, so a plain Save would otherwise reset a live strategy to paper).

A fresh clone has **no strategy store**: the app starts with an empty one and
the portal shows its "create your first strategy" form. Point
`STRATEGY_RULES_FILE` somewhere else if you keep yours outside the repo.

The Gate thresholds (`GATE_MIN_SHARPE`, `GATE_MAX_DRAWDOWN_PERCENT`,
`GATE_MIN_WIN_RATE_PERCENT`, `GATE_MAX_WEEKLY_LOSS_PERCENT`) are part of the
configuration, not the code - a strategy is judged against the bar you set.

## Documentation

| File | What it is |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | the phased build plan, with progress |
| [CHECKLIST.md](CHECKLIST.md) | task-by-task status, the Gate numbers and the open work |
| [TRAIDER_PLAN.md](TRAIDER_PLAN.md) | the architecture and design decisions |
| [DEPENDENCY_GRAPH.md](DEPENDENCY_GRAPH.md) | module dependency graph |
| [DOCUMENT_INDEX.md](DOCUMENT_INDEX.md) | index of every document |
| [SUMMARY.txt](SUMMARY.txt) | short summary of the project |
| [.github/skills/](.github/skills) | task-scoped notes (OpenBB, backtesting, features, risk, Alpaca) |

## Safety

- **Nothing here places an order yet.** The broker, credentials, the
  per-strategy paper/live switch and the trading on/off lock are configured and
  enforced, but the executor itself is still to be written.
- **Trading starts OFF, and going live is refused rather than downgraded.**
  Turning trading on is refused outright while the selected Alpaca account has no
  API keys for it (otherwise "trading on" would be a lie), and it is confirmed on
  the switch itself every time - the live prompt is about real money, the paper one
  about the strategy starting to act. Silently falling back to paper would either
  hide a broken live setup or spend real money. The header pills say which account
  and whether trading is on, and each run's `inputs.execution` records which
  environment was in play.
- **Keys must be verified, not just configured.** The environment in play has to
  have passed a real check against the broker before trading can start
  (`GET /v2/account`, one call). Presence proves nothing: keys get copied from the
  wrong account page, revoked, or paired with the other environment's secret, and
  every one of those would previously have shown as a green "trading ON" while
  every order bounced off a rejected key. Where the check happens differs by what
  is at stake: **paper is verified as part of turning trading on** (nothing to do
  first - and it is not asked twice once it has passed), while **live must have
  passed before the switch is armed**, because discovering a bad key in the same
  click that starts real orders is not a discovery worth having. The verdict is per
  environment and belongs to the KEY that earned it, so swapping keys expires it
  automatically. A pass is cached - editing an unrelated setting does not re-ask
  Alpaca - while a failure is not, since it may just be the network.
- **A key pair the broker rejects is never written down.** Saving the account form
  checks the pairs it is about to add or change, and a pair Alpaca answers 401/403
  for is left out of the account file: a stored credential that cannot work would
  make the file claim something untrue and push the failure to some later, less
  obvious moment (the first order, say). Everything else in that save lands — one bad
  pair is not a reason to discard the data folder, the S3 settings and the backtest
  defaults that arrived with it — and the popup names the pair it left behind. A pair
  that could not be *reached* is saved unproven instead, because a network problem is
  not evidence about a credential.
- **Paper is always the default.** Credentials for a live account do not move the
  switch, and neither does re-using an existing strategy: routing orders to a real
  account is an explicit choice, made in the header dropdown, per strategy.
- **No reconfiguration while trading is on.** With trading on, the server refuses
  every configuration write with HTTP 409 - settings, account, rules, strategy
  create/rename/delete/select, the backtest runner, the dataset rebuild/backfill
  and the delta sync - and the UI disables the matching buttons. A stale browser
  tab or a scripted POST cannot reconfigure a strategy mid-flight. Reads stay
  available, and turning trading off is always allowed: that is the only action
  that releases the lock, so it must not depend on the configuration it freezes.
- Keep credentials in `.env` (gitignored) or a secret manager - never in code.
  Both the account file and the trading state file are written owner-only (0600).
- A backtest is not a forecast. The Gate exists to stop a strategy that has not
  earned capital, and passing it is a floor, not a promise.
- Paper results are optimistic: the paper engine simulates no slippage, fees or
  dividends, so it will beat both your backtest and live.
- This is a personal engineering project, not financial advice.
