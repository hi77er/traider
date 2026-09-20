"""The loop: wake on a bar boundary, ask the gates, decide, act, record.

    python -m src.main        # hosts this (see src/main.py)

One tick, in this order, and the order is not arbitrary:

  1  is trading ON?            ``trading_state.is_trading_on`` — re-read EVERY tick
  1b is it the STAMPED strategy?  the one trading.json recorded when it was armed
  2  could an order be placed?  credentials resolve, and the exchange clock says open
  3  sync the dataset to now    the only provider call in the loop
  4  read the trailing window   from the DATASET, ending at the newest CLOSED bar
  5  is the stored bar current? the file must reach the bar that should have closed
  5b is the bar a bar?         ``src.data.quality`` — an impossible one refuses, an odd one is noted
  5c may anything NEW be opened?  a stale process, and the day's loss limit — entries only
  6  already decided that bar?  the driver's idempotency key
  7  decide and act             ``LiveDriver.on_bar_closed`` → reconcile → engine → broker
  8  record it                  the ledger, the state file and the live store

Three properties this module is responsible for, none of which the driver can enforce:

**One tick is one decision, and a duplicate tick is harmless.** The bar is chosen from the
data, the driver refuses to act twice on it, and nothing here replays a bar it missed. A
restart mid-bar therefore costs nothing, and a sleep that overshoots skips a decision rather
than making two.

**A tick that does nothing still says so.** Every path returns a record, and the record is
written to ``latest.json`` — the heartbeat. Without it, a quiet day and a dead loop look
identical from the dashboard, and those two want opposite responses.

**No tick may kill the loop.** Anything unexpected is caught, recorded as a refusal and
logged; the run sleeps to the next boundary and tries again. A bot that stops tiring itself
out on one bad bar is worse than one that keeps asking, because the next bar is usually fine.

**The day's loss limits belong to the loop, not to the machine.** ``MAX_LOSS_PERCENT`` and
``MAX_CONSECUTIVE_LOSSES`` are measured over the exchange DAY and against the ACCOUNT, so
they need the trade log and a broker — neither of which ``src/strategy`` may reach. The loop
gathers the facts, ``src.strategy.limits`` decides, and the verdict is handed to the driver
as a refusal to OPEN anything new. The same route carries the other thing the machine cannot
see for itself: whether this process is still running the code that is on disk. An open
position keeps its stop, its take and its signal exit in both cases: this holds back new risk,
it never holds a loser.

What this module deliberately does NOT do: hold an executor or a driver between ticks. Both
are built per tick from the settings and the stored state, so a change to the rules, the risk
settings or the environment is picked up at the next boundary without a restart — which is
the same reason the trading switch is re-read every tick.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from src.config import freshness
from src.config.effective import active_strategy_name
from src.config.trading_state import armed_strategy, get_state, is_trading_on
from src.data import dataset
from src.data import delta as delta_mod
from src.data import live as live_data
from src.data import quality
from src.data.dataset import load_dataset
from src.execution import accounts, store
from src.execution.alpaca_broker import AlpacaBroker
from src.execution.alpaca_executor import AlpacaExecutor
from src.execution.config import execution_status
from src.model.simple_model import RuleBasedSignalGenerator
from src.scheduler import lease as lease_mod
from src.strategy import limits
from src.strategy.config import StrategyConfig
from src.strategy.engine import StrategyEngine
from src.strategy.live import LiveDriver, NotEnoughHistory

logger = logging.getLogger(__name__)

__all__ = ["build_driver", "run", "tick"]

#: Actions worth a line in the day's log. The heartbeat is not one of them: a market that
#: has been shut for eight hours would otherwise write eight hours of identical lines, and
#: the log is what someone reads to find out what HAPPENED.
LOGGED_ACTIONS = frozenset({"decided", "refused"})

#: The GATES a tick walks, in the order it walks them, and the names it stamps on a record that
#: stopped at one (see ``tick``). They exist for the reader of a quiet bot: ``refused`` alone
#: covers six different reasons nothing happened, and "which gate is it stuck behind" is the
#: question that follows every one of them. A name here is a FACT about the tick, not a label —
#: the trading log renders the pipeline from the last tick's own record.
#:
#: Order matters and is the order below: a tick that stopped at ``clock`` never reached
#: ``sync``, and a reader is owed that distinction rather than a list of things that ran.
TICK_STAGES = (
    "switch",     # is trading on, re-read every tick
    "armed",      # is this the strategy the switch was armed for
    "execution",  # could an order be placed at all
    "clock",      # is the exchange open — asked of the exchange, never inferred
    "sync",       # the dataset is brought up to now
    "window",     # the trailing window is readable and not behind
    "quality",    # the bar about to be decided on is a bar at all
    "decide",     # the strategy decided, and acted on its decision
)

#: Seconds added to a boundary before asking. The provider's newest bar is not always in
#: place the instant it closes, and one tick is cheap while a missed bar is not.
PROVIDER_LAG_SECONDS = 5.0

#: How often a SLEEPING loop re-reads the switch, on a wait short enough to allow it. A bar
#: boundary can be an hour away and the wait used to be one uninterruptible call, so turning
#: trading off left a process that still said "running" in the panel until the bar closed — a
#: switch that works, looking broken. The wait is the same length; it is just no longer one
#: block.
STOP_CHECK_SECONDS = 1.0

#: ...and a ceiling on how many times one wait may be cut up. An hourly wait gets a check
#: every second — the switch feels instant, which is the point — while a daily or weekly one
#: gets a coarser slice, so a long wait is not a hundred thousand wake-ups to answer a
#: question nobody asked that precisely. All that matters operationally is that this is
#: capped: the cost of a slice is a file read, and it must not scale with the bar size.
MAX_WAIT_SLICES = 3600

#: The loop's own source: the files that decide what to trade and how. Watched by mtime so a
#: loop running older code than these refuses to OPEN anything new (see ``src/config/
#: freshness``), which is the one failure a process cannot see from the inside — every test
#: imports the code fresh, so the tests pass while this process runs yesterday's strategy.
#: Deliberately not the whole tree: a change to the dashboard or to the data pipeline is not a
#: reason to stop trading, and a watch list that fires on everything is one nobody trusts.
LOOP_SOURCE = (
    "src/scheduler/orchestrator.py",
    "src/strategy/config.py",
    "src/strategy/engine.py",
    "src/strategy/limits.py",
    "src/strategy/live.py",
)

#: The mtimes of ``LOOP_SOURCE`` as this process IMPORTED them. Python loads a module once, so
#: this is the code the loop is running; anything newer on disk than this is code it has never
#: read, however many times the file has been saved since.
LOADED_SOURCE = freshness.snapshot(LOOP_SOURCE)


def build_driver(settings, *, name: Optional[str] = None, dry_run: bool = False, broker=None) -> LiveDriver:
    """The strategy machine, wired exactly as the backtest wires it.

    Slippage and commission are left at zero: ``StrategyConfig.from_settings`` documents
    that a live run passes zero because the REAL fill from the broker is what counts. The
    cost model exists to make a simulation honest, and inventing one here would move the
    expected price away from the price that actually gets paid.
    """
    engine = StrategyEngine(StrategyConfig.from_settings(settings))
    generator = RuleBasedSignalGenerator(settings=settings)
    return LiveDriver(
        settings=settings,
        engine=engine,
        generator=generator,
        broker=broker if broker is not None else AlpacaBroker(settings),
        dry_run=dry_run,
        name=name,
    )


def _window(settings, until, need: int):
    """The trailing bars from the DATASET, ending at ``until`` — the newest closed bar.

    The dataset rather than the provider, so the chart, the backtest and the decision are
    looking at identical bars. Truncating at ``until`` is what keeps a still-forming bar out
    of the decision: the file may hold one, and a bar whose close has not happened is a
    price that is still moving.
    """
    frame = load_dataset(settings, settings.instrument, settings.historical_bar_size)
    if frame.empty:
        return frame, until
    keep = [dataset.bar_stamp(settings, ts) <= until for ts in frame.index]
    frame = frame[keep]
    if frame.empty:
        return frame, until
    newest = dataset.bar_stamp(settings, frame.index[-1])
    return frame.tail(max(1, int(need))).copy(), newest


def _bump_day(settings, name: str, day: str, **fields) -> None:
    """Count one day's activity in the index, so listing days never reads the tick log."""
    entries = {str(e.get("day")): e for e in store.load_index(settings, name)}
    entry = dict(entries.get(day, {}))
    entry.pop("day", None)          # it is the key, not a field
    for key, value in fields.items():
        if isinstance(value, int) and isinstance(entry.get(key), int):
            # Sum these: a day is looked back on for "how often", not for the last value.
            entry[key] = entry[key] + value
        else:
            entry[key] = value
    store.upsert_day(settings, name, day, **entry)


def _new_entries_blocked(settings, strategy: str, env: str, at) -> Optional[str]:
    """Why no new entry may be opened this tick, or ``None`` when nothing is in the way.

    Two independent reasons, and it is worth being clear about what they have in common: both
    are refusals to ADD risk, and neither is a reason to abandon risk already taken.

    * **the loop is running older code than the files on disk.** Python loads a module once, so
      an edit under a running loop is an edit it has never read — the strategy in flight is
      then one nobody is looking at, and every test still passes, because the tests import the
      code fresh. It keeps reconciling and it keeps its exits: the position it is holding is
      real whichever code put it there.
    * **the day's loss limits** (see :func:`_day_loss_halt`).

    Both are handled here rather than in the driver because both need facts the driver cannot
    reach — the filesystem's mtimes, the day's trades, the account's equity.
    """
    reasons: List[str] = []

    stale = freshness.info(LOOP_SOURCE, loaded=LOADED_SOURCE)
    if stale["stale"]:
        reasons.append(
            "the loop is running older code than the files on disk ("
            + ", ".join(stale["changed"])
            + " changed since it started) — restart it before it opens anything new"
        )

    loss = _day_loss_halt(settings, strategy, env, at)
    if loss:
        reasons.append(loss)

    return " ".join(reasons) or None


def _day_loss_halt(settings, strategy: str, env: str, at) -> Optional[str]:
    """Whether the day's loss limits stop new entries, and why (``src.strategy.limits``).

    Three facts, gathered HERE because this is the layer that can reach all of them: the
    day's trades from the trade log, the account's equity from the broker, and the day
    itself from the exchange's calendar. The day comes from ``store.trading_day`` and never
    from the local date: a tally that reset at local midnight would hand a losing session a
    fresh budget halfway through it.

    Reads NOTHING when neither limit is configured — no broker call, no file read, no new
    way for a run that does not use these settings to behave differently.

    ``accounts.snapshot`` is asked only when ``MAX_LOSS_PERCENT`` is set, and it answers
    with "could not be read" rather than raising; that answer is passed straight through so
    the policy can fail closed on it.

    Anything unexpected in gathering the facts is a FAILED CLOSED verdict rather than a
    shrug: a limit that cannot be evaluated is not a limit that has been satisfied.
    """
    config = StrategyConfig.from_settings(settings)
    if config.max_loss_percent is None and config.max_consecutive_losses is None:
        return None

    try:
        day = store.trading_day(settings, at)
        trades = store.read_trades(settings, strategy, when=day)
        equity = last_equity = None
        account_reason = ""
        if config.max_loss_percent is not None:
            account = accounts.snapshot(settings, env).as_dict()
            equity = account.get("equity")
            last_equity = account.get("last_equity")
            account_reason = str(account.get("reason") or "")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not evaluate the day's loss limits")
        return (
            f"the day's loss limits could not be evaluated ({exc}) — refusing new entries "
            "rather than trading without them"
        )

    return limits.day_halt_reason(
        config,
        trades=trades,
        equity=equity,
        last_equity=last_equity,
        account_reason=account_reason,
    )


def tick(
    settings,
    *,
    now=None,
    sync_call: Optional[Callable[[], Any]] = None,
    clock_call: Optional[Callable[[], Dict[str, Any]]] = None,
    driver: Optional[LiveDriver] = None,
    dry_run: bool = False,
    record: bool = True,
) -> Dict[str, Any]:
    """One tick. Never raises; always returns a record of what it did and why.

    ``sync_call``/``clock_call``/``driver`` are injectable so a test can drive a whole day
    without a provider, an exchange or a broker. In production all three are built here.
    """
    at = now or datetime.now(timezone.utc)
    strategy = active_strategy_name() or getattr(settings, "instrument", "strategy")
    env = str(getattr(settings, "execution_env", "paper") or "paper").lower()

    # Anything worth recording about the bar that is not worth refusing over. Filled in below,
    # once the bar is in hand, and read by ``finish`` from the closure — so every record from
    # that point on carries it without each of the paths having to remember. The notes belong
    # to the tick, not to the route it took out of the pipeline.
    notes: List[str] = []

    def finish(
        action: str,
        reason: str = "",
        stage: str = "",
        logged: bool = False,
        index_fields: Optional[Dict[str, Any]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        **extra,
    ) -> Dict[str, Any]:
        record_ = store.tick_record(
            strategy=strategy, env=env, action=action, reason=reason, stage=stage,
            settings=settings, at=at, notes=notes, **extra,
        )
        if record:
            try:
                store.save_latest(settings, strategy, record_)
                if logged or action in LOGGED_ACTIONS:
                    # Dated by the TICK's moment, not by the wall clock. They are the same
                    # in a live run, but a replayed or backfilled tick would otherwise be
                    # filed under today — and the file's name would disagree with the
                    # record's own ``day`` field, which is read from the same moment.
                    store.append_tick(settings, strategy, record_, when=at)
                    # The order and trade logs are the LOOP's to write: reading and writing
                    # state is the driver's job, housekeeping is not, and ``src/strategy``
                    # may not import ``src/execution`` at all. The rows come from the
                    # driver's own reports, so what lands here is what the machine tried to
                    # do — the broker's copy of the same orders answers a different
                    # question, and ``client_order_id`` is what joins the two.
                    for row in orders or []:
                        store.append_order(settings, strategy, row)
                    for leg in record_["trades"]:
                        store.append_trade(
                            settings, strategy,
                            store.trade_record(
                                settings=settings, strategy=strategy, env=env,
                                at=at, bar=record_["bar"], leg=leg,
                            ),
                        )
                    _bump_day(
                        settings, strategy, record_["day"],
                        env=env, last_action=action, last_at=record_["at"],
                        events=1, **(index_fields or {}),
                    )
            except Exception:  # noqa: BLE001 - a failed log must not stop the bot
                logger.exception("Could not record the tick — continuing")
        return record_

    # -- 1. the switch ------------------------------------------------------
    # Re-read every tick and never cached: this is what makes OFF take effect at the next
    # boundary with no signal to this process and no network involved.
    if not is_trading_on(settings):
        return finish("off", "trading is OFF", stage="switch")

    # -- 1b. is this the strategy it was armed for? -------------------------
    # The loop runs the STAMPED strategy, not whatever happens to be active now. If they
    # differ, something changed the active strategy behind the switch's back — a stale tab,
    # a hand-edited store, or an endpoint that forgot the lock — and the honest response is
    # a loud refusal rather than orders for a strategy nobody armed.
    stamped = get_state(settings).get("strategy")
    if stamped and stamped != strategy:
        return finish(
            "refused",
            f"trading was armed for {stamped!r} but the active strategy is {strategy!r} — "
            "refusing to trade a strategy that was not armed. Turn trading off and on again.",
            stage="armed",
        )

    # -- 2. could an order be placed at all? --------------------------------
    status = execution_status(settings)
    if not status.get("ok"):
        return finish("refused", f"orders would be refused — {status.get('message')}", stage="execution")

    try:
        clock = (clock_call or _default_clock(settings))()
    except Exception as exc:  # noqa: BLE001 - an unreadable clock is a refused tick
        # Fail the tick. Falling back to a weekday-and-hours check is how a holiday trades a
        # stale bar, and the exchange is the only thing that actually knows. Caught broadly
        # and on purpose: the SDK raises whatever its transport raised, and an exception
        # that escapes here would take the whole loop down over one unanswered request.
        logger.exception("The exchange clock could not be read")
        return finish("refused", f"the exchange clock could not be read ({exc})", stage="clock")
    if not clock.get("is_open"):
        return finish("closed", "the exchange is closed", stage="clock")

    # -- 3. sync the dataset to now ----------------------------------------
    # The only provider call in the loop, and already throttled. It happens even when the
    # decision below turns out to be a no-op, so a loop that has been off for a week comes
    # back to a current dataset instead of a gap it can never catch up on.
    try:
        (sync_call or (lambda: delta_mod.sync_missing_days(settings)))()
    except Exception as exc:  # noqa: BLE001 - a provider outage is a refused tick
        return finish("refused", f"the dataset could not be synced ({exc})", stage="sync")

    # -- 4. read the trailing window from the dataset ----------------------
    until = dataset.bar_stamp(settings, dataset.last_closed_bar(settings, at))
    try:
        # The driver's own history requirement, asked of the module that defines it rather
        # than by building a driver just to read a number off it.
        window, newest = _window(settings, until, live_data.required_bars(settings))
    except Exception as exc:  # noqa: BLE001
        return finish("refused", f"the dataset could not be read ({exc})", stage="window")

    if window.empty:
        return finish("refused", "the dataset has no bars up to the last closed bar — backfill first", stage="window")
    if newest < until:
        # The newest bar in the file is BEHIND the bar that should have closed. Trading it
        # would be acting on a price the market has already moved past, so this refuses
        # loudly instead — the failure that costs nothing instead of the one that costs money.
        return finish(
            "refused",
            f"the newest stored bar is {newest} but {until} should have closed — "
            "the dataset is behind; refusing to decide on a stale bar",
            stage="window",
        )

    # -- 5b. is the bar a bar? ---------------------------------------------
    # The decision below is made on the newest CLOSED bar, so the question here is whether
    # that bar describes a market that could have existed. One that cannot be — a missing
    # price, a zero, a high under its low — refuses the tick, because a decision made on an
    # impossible bar is a decision about a market that never happened, and every number
    # downstream of it inherits the mistake.
    #
    # A bar that is merely STRANGE is not refused. It is recorded as a note and the decision
    # goes ahead: the line is "provably impossible" against "worth a look", because anything
    # softer would need a threshold nobody configured, and that is a trading rule — which
    # belongs in the strategy, where a backtest can measure it.
    signal_row = window.iloc[-1]
    broken = quality.broken_reason(signal_row)
    if broken:
        return finish("refused", broken, stage="quality")
    notes.extend(quality.notes(signal_row))

    # -- 5c. may anything NEW be opened? -----------------------------------
    # Before the driver is built, because the answer changes what it is allowed to open — and
    # as a VETO rather than a refusal, because an entry is not the only thing a tick does: an
    # exit the broker made must still be adopted and booked below, or the bot spends the rest
    # of the day believing it holds a position it does not.
    halt = _new_entries_blocked(settings, strategy, env, at)

    # -- 5 & 6. decide and act ---------------------------------------------
    wanted = armed_strategy(settings)
    driver = driver or build_driver(settings, name=wanted, dry_run=dry_run)
    if driver.name != wanted:
        # A caller handed us a driver for a different strategy. Better to say so than to
        # trade through it: the state file and the account would both be the wrong one.
        return finish("refused", f"the driver is for {driver.name!r}, not {wanted!r}", stage="decide")
    # The verdict is the LOOP's, so it is applied to whichever driver the tick ended up
    # with — including one injected by a test or a replay. A driver cannot disagree with
    # the loop about whether today is halted, and the loop cannot be bypassed by passing it
    # a driver that was built before the tally was taken.
    driver.halt = halt

    try:
        result = driver.on_bar_closed(window)
    except NotEnoughHistory as exc:
        return finish("refused", str(exc), stage="decide")
    except Exception as exc:  # noqa: BLE001 - one bad bar must not stop the loop
        logger.exception("Tick failed while deciding")
        return finish("refused", f"the decision failed ({exc})", stage="decide")

    if result.get("action") == "noop":
        return finish("noop", result.get("reason") or "this bar was already decided",
                      stage="decide", bar=result.get("bar"))
    if result.get("action") == "refused":
        # A refusal can still have BOOKED something: an exit the broker made is adopted
        # before the reconcile that refuses, so the trade happened whatever the tick's
        # verdict. Dropping it here is how a real exit vanishes from the log.
        legs = result.get("trades") or []
        return finish(
            "refused",
            result.get("reason") or "the driver refused",
            stage="decide",
            bar=result.get("bar"),
            index_fields={"trades": len(legs)} if legs else None,
            trades=legs,
        )

    # -- 7. record ---------------------------------------------------------
    intents = result.get("intents") or []
    order_ids = [i.get("order_id") for i in intents if i.get("order_id")]
    if result.get("adopted"):
        order_ids.append(result["adopted"].get("order_id"))
    order_ids = [oid for oid in order_ids if oid]
    legs = result.get("trades") or []
    # One row per submitted order, including the ones that were refused: an order that did
    # not fill is exactly what someone reading the log is looking for, and a log that only
    # records successes cannot answer "why is this bar missing a trade".
    orders = [
        store.order_record(
            settings=settings, strategy=strategy, env=env, at=at,
            bar=result.get("bar"), intent=intent,
        )
        for intent in intents
        if not intent.get("skipped") and intent.get("intent")
    ]
    position = driver.state.position
    return finish(
        "decided",
        # The driver's own reason, when it has one: with the day's loss limit in force the
        # tick decided, refused the entry, and this is where it says so. An empty reason is
        # the ordinary case.
        reason=result.get("reason") or "",
        stage="decide",
        logged=True,
        index_fields={"decided": 1, "orders": len(orders), "trades": len(legs)},
        orders=orders,
        bar=result.get("bar"),
        signal=result.get("signal"),
        intents=intents,
        trades=legs,
        adopted=result.get("adopted"),
        position=None if position is None else position.as_dict(),
        order_ids=order_ids,
    )


def _default_clock(settings) -> Callable[[], Dict[str, Any]]:
    """The exchange clock, built here so the tick does not resolve credentials itself."""
    def call() -> Dict[str, Any]:
        return AlpacaExecutor(settings).clock()

    return call


def _wait_for_the_boundary(seconds: float, sleep: Callable[[float], None], settings) -> bool:
    """Sleep ``seconds``, returning False when trading was turned OFF while waiting.

    Sliced rather than one call, so the switch can stop the loop between ticks as well as
    during one. Nothing else about the wait changes: the total is the same, the lease was
    already refreshed with the boundary it committed to, and a loop that wakes late still
    skips the bar it missed rather than replaying it.
    """
    remaining = float(seconds)
    # Fixed from the TOTAL, not recomputed as it shrinks: a step that shrank with the remainder
    # would decay towards nothing and take far more slices than the ceiling allows.
    step = max(STOP_CHECK_SECONDS, remaining / MAX_WAIT_SLICES)
    while remaining > 0:
        # Checked BEFORE sleeping, so the first slice of an already-off switch costs nothing.
        if not is_trading_on(settings):
            return False
        now = min(step, remaining)
        sleep(now)
        remaining -= now
    return True


def run(
    settings,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    ticks: Optional[int] = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    lease=None,
    resolve: Optional[Callable[[], Any]] = None,
    **tick_kwargs,
) -> List[Dict[str, Any]]:
    """Sleep to each bar boundary and tick. Returns the records it produced.

    ``ticks`` limits how many ticks to run — the whole loop is testable with ``ticks=1``
    without waiting for a boundary, and it is what a cron deployment would call.

    ``clock`` is a CALLABLE returning now, while ``tick``'s ``now`` is a moment. They are
    named apart deliberately: passing one where the other was meant is silent, and the
    loop would then tick on a wall clock while its caller believed it was on a fixed one.

    ``lease`` is the loop's claim on the host, refreshed BEFORE each sleep with the boundary
    it is about to sleep until. Declaring the wake in advance is what lets a later reader
    tell "asleep until 14:30" from "died at 14:05" — the loop is asleep for an hour at a
    time on purpose, so a heartbeat that needed a timer to prove it was alive would fight
    the scheduling this is built around. Optional, because a test drives the loop with no
    lease at all.

    ``resolve`` re-reads the settings for each tick. The account layer answers from files
    with an mtime-keyed cache, so passing it is what makes a change made in the dashboard —
    a risk setting, the environment, the bar size — take effect at the next boundary with
    no restart and no IPC. Without it the loop keeps the settings it was handed.

    A tick is always followed by a sleep, including a failed one: the next bar is usually
    fine, and a loop that spins on a broken configuration is a loop that fills the disk with
    the same refusal.
    """
    records: List[Dict[str, Any]] = []
    while ticks is None or len(records) < int(ticks):
        at = clock()
        current = resolve() if resolve is not None else settings
        try:
            records.append(tick(current, dry_run=dry_run, now=at, **tick_kwargs))
        except Exception:  # noqa: BLE001 - nothing may kill the loop
            logger.exception("Tick raised outside its own handling; continuing")

        # The switch OWNS this process. Turning trading off is meant to stop the loop, not to
        # leave it idling until someone notices the chip still says "running" — and a stopped
        # loop must not be restarted by hand either, because arming starts it
        # (``src/web/services/loop_control``). Checked here as well as during the sleep, so a
        # run whose switch is already off costs one tick and not one bar.
        if not is_trading_on(current):
            logger.info("Trading is OFF — stopping the loop")
            break

        if ticks is not None and len(records) >= int(ticks):
            break

        boundary = dataset.next_bar_boundary(current, at)
        if boundary is None:
            logger.error("Could not work out the next bar boundary — stopping rather than spinning")
            break

        if lease is not None:
            # Before the sleep, never after: the claim has to be honest about the interval
            # it is about to be unresponsive for.
            lease_mod.refresh(lease, next_wake=boundary)

        wait = (boundary - at).total_seconds() + PROVIDER_LAG_SECONDS
        logger.info("Next bar closes at %s — sleeping %.0fs", boundary, max(0.0, wait))
        if wait > 0:
            if not _wait_for_the_boundary(wait, sleep, current):
                logger.info("Trading is OFF — stopping the loop")
                break
        else:
            # Behind the boundary: do not catch up, just move on to the next one. Replaying
            # the bar we are late for would place an order at a price that no longer exists.
            logger.warning("Woke %.0fs late for %s — skipping it, not replaying it", -wait, boundary)

    return records
