# The execution loop — design and build plan

The live trading loop: a separate process that wakes on bar boundaries, decides with
the SAME strategy machine the backtest drives, and places orders through the Alpaca
broker. This document is the agreed design plus the order it gets built in, so the
reasoning survives the code.

Status: **design settled 2026-09-16**. Phases 0–3 are built; 4–8 are not.

Related reading: [`README.md`](../README.md) ("Two processes"), `TRAIDER_PLAN.md`
(phases and the file tree), `src/strategy/live.py` (the driver), and
`src/execution/` (the broker).

---

## 1. Why a process, and what triggers it

Three different questions get confused with each other, and keeping them apart is
the whole of this section:

| Question | Answer |
| --- | --- |
| What LAUNCHES the loop? | The OS. A container entrypoint, a terminal, a supervisor. `src/main.py` is the host, not a trigger. |
| What WAKES a tick? | An internal sleep to the next **bar boundary** — not a fixed interval, not cron. |
| What makes a tick DO anything? | The gates, in order: trading ON → market open → inside the decision window → not already decided. |

**Turning trading ON does not start the loop.** The loop runs permanently and reads
the switch before every tick, so ON is a flag the next boundary picks up, and OFF
takes effect at the next boundary without touching the network. That is what makes
the stop instant and free of dependencies on the broker.

Two consequences the implementation must honour:

- **Idempotent per bar.** `state.last_decided_bar` is the key, so a restart mid-bar or
  a duplicate wake is harmless.
- **A lease file.** Two loops mean double orders. The lease (pid + heartbeat) is what
  makes "exactly one trading process" true rather than merely intended.

The process model is **two processes sharing files and nothing else** — the loop and
the dashboard. See README, "Two processes"; `tests/test_architecture.py` enforces it.

## 2. The tick, in order

| # | Step | Code |
| --- | --- | --- |
| 1 | Is trading armed? | `src.config.trading_state.is_trading_on` — re-read EVERY tick, never cached |
| 2 | Is the exchange open? | Alpaca `GET /v2/clock` via `AlpacaExecutor.clock()` |
| 3 | Is the closed bar inside the decision window? | `src/config/session.py` |
| 4 | Sync the dataset to now | `delta.sync_missing_days` — the ONLY provider call in the loop |
| 5 | Read the trailing window | dataset FIRST (≥ `LiveDriver.required_bars`); `get_history_window` only as a fallback |
| 6 | Already decided this bar? | `LiveDriver.state.last_decided_bar` |
| 7 | Reconcile with the broker | `LiveDriver.adopt_broker_exit` then `reconcile` |
| 8 | Decide | `on_bar_closed` → features → generator → `StrategyEngine.step` |
| 9 | Place / flatten | `AlpacaBroker.submit` → bracket, then amend the exits after the fill |
| 10 | Record | ledger + `save_state` + the live store |

Order is not incidental. Reconcile precedes decide so the engine never sizes against
a stale view of what is held; state is saved last so a crash mid-tick leaves the bar
undecided and therefore retryable.

### Scheduling

- **Two clocks, two questions.** "May an order be placed now?" is the exchange's
  answer (`/v2/clock`, which knows holidays and half-days). "Should this strategy act
  on this bar?" is the configured window. Both must pass, in that order.
- **If the clock call fails, fail the tick.** Falling back to a local weekday check is
  exactly how a holiday trades a stale bar.
- **No calendar is stored anywhere.** A cached calendar goes stale silently; only
  `next_close` is remembered, and only long enough to schedule the next wake.
- **Wake on bar boundaries** (+~5s for provider lag). A boundary-aligned sleep loop
  beats a cron library here: simpler, and testable without waiting.
- **No catch-up.** Woken late, or down across N bars, decide only on the NEWEST closed
  bar and log the gap. Replaying stale bars means orders at prices that no longer exist.
- **The decision bar is derived from the data**, not from arithmetic on the wall clock:
  signal on the newest closed bar, fill at the next bar's open. That matches the
  backtest exactly and survives a provider that labels bars differently.
- **Startup:** acquire the lease → one reconcile against the broker, reported and not
  traded on → sleep to the next boundary. A position may exist while trading is OFF;
  it is never replayed.

## 3. Gates — the policy

| Gate | Behaviour |
| --- | --- |
| `turn_on` | **Refuse while ANY position is open**, naming it. **Stamp the strategy name** into the ON state |
| `POST /execution/env` (paper↔live) | **Refuse while any position is open** — otherwise the position is orphaned in the other account |
| `turn_off` | **ALWAYS allowed.** Never require flat, never require the network |
| "Stop trading & flatten" | One deliberate action: OFF + `AlpacaExecutor.flatten()` |

**"Refuse to arm while any position is open" is a decision, not a default.** It was
chosen as the answer to *"what if I switch the active strategy while a position is
open?"* because it is the simplest option and because it makes **"only the active
strategy trades" literally true of the position** as well as of new entries.

Consequences:

- **`/rules/select` requires flat.** If the strategy could be switched mid-position,
  the subsequent flatten would run against the NEW strategy's instrument and
  `EXECUTION_ENV` and miss the position. Blocking the switch is what keeps "flatten
  first" reachable from the owning strategy's own screen.
- The refusal **names the position** and gives both ways out: *"Trading cannot start —
  **Alpha** holds 90 AAPL. Flatten them first (Stop & flatten), or wait: the resting
  bracket may close it on its own."*
- **"Or wait" needs no cleanup**, because the gate reads the **broker**. A position
  closed by its bracket is simply gone by the next attempt; nothing has to tick while
  trading is OFF for that to be true.
- **`require_flat` guards the three orphan-prone actions** — `/execution/env`,
  `/rules/select`, `/rules/delete` — with a 5–10s positions cache. An unreachable
  broker is a refusal **with the reason**, never treated as flat.
### When is an account flat? Three answers, and only three

The gate fails closed, but not blindly. "Unreachable" and "empty" look identical from the
outside, so the distinction is made from the **configuration** rather than from a guess:

| Situation | What it proves | Gate |
| --- | --- | --- |
| No credentials for that environment | no order could have been placed there | **allow** — flat by construction |
| Credentials configured, broker answers | what is actually held | allow if flat, refuse if not |
| Credentials configured, broker unreachable | nothing | **refuse**, with the reason |
| Credentials half-configured (key, no secret) | nothing | **refuse**, with the reason |

"Flat by construction" is a proof, not an assumption: it is what keeps a data-only install
(the README's default) able to switch strategies without Alpaca keys at all. Everything
else refuses, because a 401 is indistinguishable from an empty account to every caller
downstream.

### Both accounts are read, not just the one in play

Paper and live are different accounts, and switching paper→live strands a **paper** position
just as thoroughly as the reverse. A check that only looked at the environment in play would
refuse a switch for a reason it cannot see. An environment with no credentials is flat by
construction and costs nothing, so covering both is free until both are genuinely configured.- **The lock follows EXPOSURE, not the switch.** `require_trading_off` stays cheap and
  local for the frequent settings writes.
- **The loop trades the STAMPED strategy** and refuses on mismatch, so a stale tab, a
  hand-edited store or a future endpoint that forgets the dependency produces a loud
  refusal rather than orders for the wrong strategy.
- **OFF does not mean flat.** The copy says so, an **"N open"** header pill (from the
  broker) keeps it visible, and the turn-off message distinguishes three cases: no
  positions / open with working exits / open with **no** exits (the loud one — an empty
  `STOP_LOSS_PERCENT` means no stop, so no bracket).

**Rejected: OFF-requires-flat.** It would cost the one-press stop, make the bot harder
to stop than to start, and let a broker outage trap it in ON. `turn_on` plus the env
switch requiring flat already close the orphan hole.

**Rejected: a per-strategy armed flag.** A second switch can disagree with the first —
the same shape that was removed from the risk layer.

## 4. Storage

**One tree, `data/live_results/<strategy-slug>/` — NOT split paper/live.** The mode is
a property of each RECORD, so splitting would break paper-vs-live comparison (the whole
point of paper trading), force history to move on promotion, and scatter one strategy's
timeline.

```
data/live_results/<strategy-slug>/
  latest.json               # the panel's view: the LAST tick, whatever it did
  index.json                # one record per trading day
  ticks/<date>.jsonl        # append-only: bar key, signal, intents, refusals
  orders.jsonl              # every submit: env, client_order_id, status, fill
  trades.jsonl              # closed round trips (the ledger)
  state-<env>.json          # driver state, PER ENVIRONMENT
```

- **State is keyed by (strategy, env), and both halves matter.** The environment because
  paper and live are different ACCOUNTS — a shared file reconciles a paper position against
  the live account and refuses for ever. The strategy because this used to be keyed by the
  INSTRUMENT alone, which gave two strategies on the same symbol one position between them.
- **The day is the MARKET's day**, not UTC: `ticks/<date>.jsonl` and the index are keyed by
  the date in `MARKET_TIMEZONE`, so one session is one file rather than two halves either
  side of midnight. The same boundary is what the deferred loss limits reset on.
- **`latest.json` is written on EVERY tick, including the ones that do nothing.** It carries
  the heartbeat, so a quiet day — no new bar, market closed, or trading off — still moves it.
  Without that, "alive with nothing to do" and "dead" look identical from the dashboard, and
  those two want opposite responses. The `.jsonl` logs get a line only when something
  happened, because they are events.
- **`.jsonl` for the logs.** A rewritten array loses its tail on a crash — exactly when the
  tail is wanted — and a reader must skip a line it cannot parse, because the loop may be
  mid-append and a torn final line is normal rather than corrupt.
- `src/execution/store.py` builds this on `src/config/artifacts.py` and **does not import
  `src/backtest/`**: the trading process must not depend on the backtester. The one path the
  strategy layer shares is `artifacts.live_state_path`, because `src/strategy` must not
  import `src/execution` either — the broker is injected as a protocol precisely to avoid
  that.
- **The loop writes this tree; the dashboard only reads it.** A second writer is how the two
  processes end up disagreeing about what happened, with nobody able to say which was right.
- `trading.json` and `credential_checks.json` stay where they are — account/global, not
  per-strategy.

## 5. The broker is the source of truth

| Question | Source |
| --- | --- |
| What is held? Which orders are working? What filled, and at what price? | **Alpaca** |
| Why did it happen — which signal, which bar, which refusal? | **Local logs** |

So the trading log screen renders account state first and local context second, and
must degrade gracefully when the local log is missing. It needs two read-only
endpoints — `GET /api/v1/positions`, `GET /api/v1/orders` — whose client methods
already exist.

The same rule appears inside the driver: an exit the broker made on its own is
**adopted** (booked at the price the broker's order history reports), never
re-derived locally, because re-deriving would be a guess about the broker's behaviour
rather than a record of it.

## 6. Data and providers

- **The provider is out of the tick path.** The loop reads the already-synced dataset,
  so the chart, the backtest and the live decision see identical bars. The delta sync
  is the only provider caller and it is already throttled.
- **Rate limits are therefore a backfill question, not a trading risk.** Volume is
  small: ≈7 fetches/day at 1h bars. yfinance publishes no limit (it IP-throttles);
  Alpaca documents 200 req/min, on a separate host from the orders API.
- **Alpaca as the data provider is possible with no code change** (OpenBB has an
  `alpaca` provider, so it is `OPENBB_PROVIDER=alpaca`), but the free tier is **IEX
  only** — one exchange, not the consolidated tape — so the OHLC and volume differ,
  which changes the dataset and therefore every stored backtest. SIP is paid.
  **Never blend providers mid-dataset**: switch fully and re-backfill.

## 7. What the loop deliberately does not do

- No catch-up replay of missed bars.
- No loop inside the FastAPI lifespan, and no cron library.
- No provider call on the hot path.
- No split paper/live result trees.
- No second master switch, and no second clock.

---

## 8. Build plan

### Phase 0 — foundations ✅ DONE

| # | Step | State |
| --- | --- | --- |
| 0.1 | This document, linked from `TRAIDER_PLAN.md` | ✅ |
| 0.2 | `src/config/artifacts.py`: `slug`, `jsonable`, `write_json_atomic`, `output_root`, lifted from `src/backtest/store.py`, which re-exports them | ✅ |
| 0.3 | `SCHEDULER_ENABLED` and `SCHEDULER_TIMEZONE` retired — neither was read by anything, and a second "is the bot on" switch can only disagree with the trading switch; the loop takes its clock from the exchange | ✅ |
| 0.4 | `LIVE_LOOKBACK_DAYS` reconciled with the derived window: the DERIVED bar count is authoritative and the setting is a floor, so no configuration of it can produce a decision on too little history | ✅ |

### Phase 1 — the two driver defects ✅ DONE

| # | Step | State |
| --- | --- | --- |
| 1.1–1.2 | `ClosingFill` and `Broker.closing_fill(short)`; `AlpacaBroker` reads what closed the position out of the order history | ✅ |
| 1.3 | `LiveDriver.adopt_broker_exit` books a provable broker-side exit through `engine.settle` instead of refusing for ever | ✅ |
| 1.4 | `reconcile` still refuses for genuine drift: a position only one side knows about, an unprovable close, or a direction the two disagree on | ✅ |
| 1.5–1.6 | `AlpacaClient.replace_order` (PATCH), `AlpacaExecutor.amend_exits`, `Broker.reprice_exits`: the resting exits are moved onto the levels the REAL fill implies | ✅ |

### Phase 2 — the gates ✅ DONE

| # | Step | State |
| --- | --- | --- |
| 2.1 | `src/execution/positions.py`: a cached, both-accounts positions reader, with the three-ways-to-be-flat rule above | ✅ |
| 2.2 | `trading_service.require_flat(action)` — a factory, so each route says what it is refusing | ✅ |
| 2.3–2.5 | `require_flat` on `/execution/env`, `/rules/select`, `/rules/delete` | ✅ |
| 2.6 | `turn_on` stamps the active strategy name into `trading.json` | ✅ |
| 2.7 | `turn_on` refuses while any position is open, naming it and giving both ways out | ✅ |
| 2.8 | `POST /trading/off-flatten` + the panel button: OFF first, then `flatten()` | ✅ |
| 2.9 | `turn_off` reports what it left behind — nothing / open and protected / open with **no** exit | ✅ |

Two consequences worth knowing:

- **The panel stays on screen when trading is OFF and something is open.** That is the
  OFF≠flat point made visible: it is exactly where the flatten button is needed, and hiding
  it would be the screen agreeing with a wrong assumption.
- **The suite is now offline by construction.** `tests/conftest.py` patches the gate's single
  network door AND closes `requests.Session.request` outright, because the gate's first
  version made a *real* call with dummy keys — and a 401 is indistinguishable from an empty
  account, so that accident silently turns "a position is open" into "flat".

### Phase 3 — storage ✅ DONE

| # | Step | State |
| --- | --- | --- |
| 3.1 | `src/execution/store.py` over `data/live_results/<strategy-slug>/` | ✅ |
| 3.2 | `latest.json`, `index.json`, `ticks/`, `orders.jsonl`, `trades.jsonl` | ✅ |
| 3.3 | State keyed by (strategy, env), via `artifacts.live_state_path` | ✅ |
| 3.4 | A legacy `strategy_state_<name>.json` is moved to `_legacy/` and never adopted | ✅ |
| 3.5 | `latest.json` written every tick (the heartbeat); `.jsonl` only for events | ✅ |
| 3.6 | Tests pinning that the store does not import `src/backtest`, and that the web layer never writes it | ✅ |
| 3.7 | Every read tolerates a missing file or a torn final line | ✅ |

Two things this phase had to fix on the way through, both found by reading the code:

- **The state file was keyed by the INSTRUMENT.** `LiveDriver` derived its identity from
  `settings.instrument` and nothing ever passed a name, so two strategies on one symbol
  shared a single position — latent, since no two stored strategies currently share one, but
  it would have carried a position across a strategy switch.
- **There were two sanitisers and they disagreed.** ``live._safe`` produced
  ``My_Strategy``/``Delta___NVDA_-_1h`` where ``artifacts.slug`` produces
  ``My-Strategy``/``Delta-NVDA-1h``, so the state file would have landed in a different
  directory from the results. ``_safe`` now delegates to ``slug``.

`LIVE_DIR` was added to `Settings` for this phase to work as documented: the live tree is
derived as `<DATA_DIR>/live_results` exactly like its `historical/` and `backtest_results/`
siblings, and appears read-only in the account panel beside them.

### Phase 4 — the loop

| # | Step |
| --- | --- |
| 4.1 | `src/scheduler/orchestrator.py`: a `tick()` that returns a report and a `run()` that sleeps to boundaries |
| 4.2–4.6 | The tick's first five steps: switch → clock → window → delta sync → dataset-first window |
| 4.7 | Detect whether a NEW bar closed; log a no-op when it did not |
| 4.8 | Skip on `last_decided_bar` |
| 4.9 | Reconcile (with adoption) before deciding |
| 4.10–4.12 | Decide, act, record — in that order, state saved last |
| 4.13 | A per-tick record: PAPER/LIVE label, bar key, signal, intents, order ids, refusals |
| 4.14 | No catch-up: decide on the newest closed bar and log the gap |

### Phase 5 — the host

| # | Step |
| --- | --- |
| 5.1–5.2 | The lease in `data/loop.lock`, with a stale timeout so a crashed loop needs no manual cleanup |
| 5.3 | Compute the next wake from the exchange's own schedule, not local time |
| 5.4 | Startup: lease → a reported reconcile → sleep |
| 5.5 | `src/main.py` runs the orchestrator, keeping the host guard already built |
| 5.6 | `--once` for the suite and for a cron deployment |

### Phase 6 — dashboard surface

| # | Step |
| --- | --- |
| 6.1–6.2 | `GET /api/v1/positions`, `GET /api/v1/orders` |
| 6.3 | `GET /api/v1/loop`: lease holder, last-tick age, next wake, last refusal |
| 6.4 | The "N open" header pill, from the broker |
| 6.5 | The trading log screen: account state first, local context second, tolerant of a deleted log |
| 6.6 | The Live panel: last tick, position with its exits, environment, the session's ticks |
| 6.7 | The loud case surfaced: a position held with NO resting exits |

### Phase 7 — deferred limits and data quality

| # | Step |
| --- | --- |
| 7.1 | `MAX_LOSS_PERCENT` / `MAX_CONSECUTIVE_LOSSES`: a per-day tally of realised fills, reset on the exchange's day boundary |
| 7.2 | Refusal via the existing vocabulary — `Intent(action=SKIP)` + `Ledger.record_skip` |
| 7.3 | Decide the fate of `src/risk/circuit_breaker.py` and `src/risk/validator.py`: built, tested, imported by nothing |
| 7.4 | Refuse a bar with missing/zero/inverted high-low, or one older than the boundary that should have closed it |
| 7.5 | Refuse an entry when equity cannot be read |
| 7.6 | Refuse to tick on `freshness.info()` — a loop running older code than its own source |

### Phase 8 — deployment follow-through

| # | Step |
| --- | --- |
| 8.1 | A real log destination for the loop (a daemon's stdout is usually lost) |
| 8.2 | `docker/entrypoint.sh` starts both processes, with `main.py` as the parent |
| 8.3 | A healthcheck that can actually fail (`/api/v1/health`) |
| 8.4 | Only if the two ever become separate containers: a compose file with a shared volume |

### Definition of done

A paper account trades a full session with no manual intervention; a restart mid-bar
changes nothing; a resting bracket stop that fires between ticks is adopted rather than
wedging the bot; the stop that rests is the stop the backtest modelled; and every tick
can be explained from the record it left behind.
