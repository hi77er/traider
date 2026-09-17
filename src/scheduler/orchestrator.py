"""The loop: wake on a bar boundary, ask the gates, decide, act, record.

    python -m src.main        # hosts this (see src/main.py)

One tick, in this order, and the order is not arbitrary:

  1  is trading ON?            ``trading_state.is_trading_on`` — re-read EVERY tick
  1b is it the STAMPED strategy?  the one trading.json recorded when it was armed
  2  could an order be placed?  credentials resolve, and the exchange clock says open
  3  sync the dataset to now    the only provider call in the loop
  4  read the trailing window   from the DATASET, ending at the newest CLOSED bar
  5  is the stored bar current? the file must reach the bar that should have closed
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

from src.config.effective import active_strategy_name
from src.config.trading_state import get_state, is_trading_on
from src.data import dataset
from src.data import delta as delta_mod
from src.data import live as live_data
from src.data.dataset import load_dataset
from src.execution import store
from src.execution.alpaca_broker import AlpacaBroker
from src.execution.alpaca_executor import AlpacaExecutor
from src.execution.config import execution_status
from src.model.simple_model import RuleBasedSignalGenerator
from src.scheduler import lease as lease_mod
from src.strategy.config import StrategyConfig
from src.strategy.engine import StrategyEngine
from src.strategy.live import LiveDriver, NotEnoughHistory

logger = logging.getLogger(__name__)

__all__ = ["armed_strategy", "build_driver", "run", "tick"]

#: Actions worth a line in the day's log. The heartbeat is not one of them: a market that
#: has been shut for eight hours would otherwise write eight hours of identical lines, and
#: the log is what someone reads to find out what HAPPENED.
LOGGED_ACTIONS = frozenset({"decided", "refused"})

#: Seconds added to a boundary before asking. The provider's newest bar is not always in
#: place the instant it closes, and one tick is cheap while a missed bar is not.
PROVIDER_LAG_SECONDS = 5.0


def armed_strategy(settings) -> str:
    """The strategy this loop runs: the one the switch was armed with, else the active one.

    The same rule the tick applies when it decides whether to refuse, in the one place a
    host can ask it — the startup reconcile needs a driver before any tick has run, and
    building it for a different strategy than the first tick would use would report on an
    account the loop is not about to trade.
    """
    stamped = get_state(settings).get("strategy")
    if stamped:
        return str(stamped)
    return str(active_strategy_name() or getattr(settings, "instrument", "strategy"))


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

    def finish(
        action: str,
        reason: str = "",
        logged: bool = False,
        index_fields: Optional[Dict[str, Any]] = None,
        **extra,
    ) -> Dict[str, Any]:
        record_ = store.tick_record(
            strategy=strategy, env=env, action=action, reason=reason,
            settings=settings, at=at, **extra,
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
        return finish("off", "trading is OFF")

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
        )

    # -- 2. could an order be placed at all? --------------------------------
    status = execution_status(settings)
    if not status.get("ok"):
        return finish("refused", f"orders would be refused — {status.get('message')}")

    try:
        clock = (clock_call or _default_clock(settings))()
    except Exception as exc:  # noqa: BLE001 - an unreadable clock is a refused tick
        # Fail the tick. Falling back to a weekday-and-hours check is how a holiday trades a
        # stale bar, and the exchange is the only thing that actually knows. Caught broadly
        # and on purpose: the SDK raises whatever its transport raised, and an exception
        # that escapes here would take the whole loop down over one unanswered request.
        logger.exception("The exchange clock could not be read")
        return finish("refused", f"the exchange clock could not be read ({exc})")
    if not clock.get("is_open"):
        return finish("closed", "the exchange is closed")

    # -- 3. sync the dataset to now ----------------------------------------
    # The only provider call in the loop, and already throttled. It happens even when the
    # decision below turns out to be a no-op, so a loop that has been off for a week comes
    # back to a current dataset instead of a gap it can never catch up on.
    try:
        (sync_call or (lambda: delta_mod.sync_missing_days(settings)))()
    except Exception as exc:  # noqa: BLE001 - a provider outage is a refused tick
        return finish("refused", f"the dataset could not be synced ({exc})")

    # -- 4. read the trailing window from the dataset ----------------------
    until = dataset.bar_stamp(settings, dataset.last_closed_bar(settings, at))
    try:
        # The driver's own history requirement, asked of the module that defines it rather
        # than by building a driver just to read a number off it.
        window, newest = _window(settings, until, live_data.required_bars(settings))
    except Exception as exc:  # noqa: BLE001
        return finish("refused", f"the dataset could not be read ({exc})")

    if window.empty:
        return finish("refused", "the dataset has no bars up to the last closed bar — backfill first")
    if newest < until:
        # The newest bar in the file is BEHIND the bar that should have closed. Trading it
        # would be acting on a price the market has already moved past, so this refuses
        # loudly instead — the failure that costs nothing instead of the one that costs money.
        return finish(
            "refused",
            f"the newest stored bar is {newest} but {until} should have closed — "
            "the dataset is behind; refusing to decide on a stale bar",
        )

    # -- 5 & 6. decide and act ---------------------------------------------
    wanted = armed_strategy(settings)
    driver = driver or build_driver(settings, name=wanted, dry_run=dry_run)
    if driver.name != wanted:
        # A caller handed us a driver for a different strategy. Better to say so than to
        # trade through it: the state file and the account would both be the wrong one.
        return finish("refused", f"the driver is for {driver.name!r}, not {wanted!r}")

    try:
        result = driver.on_bar_closed(window)
    except NotEnoughHistory as exc:
        return finish("refused", str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad bar must not stop the loop
        logger.exception("Tick failed while deciding")
        return finish("refused", f"the decision failed ({exc})")

    if result.get("action") == "noop":
        return finish("noop", result.get("reason") or "this bar was already decided", bar=result.get("bar"))
    if result.get("action") == "refused":
        return finish("refused", result.get("reason") or "the driver refused", bar=result.get("bar"))

    # -- 7. record ---------------------------------------------------------
    intents = result.get("intents") or []
    order_ids = [i.get("order_id") for i in intents if i.get("order_id")]
    if result.get("adopted"):
        order_ids.append(result["adopted"].get("order_id"))
    order_ids = [oid for oid in order_ids if oid]
    position = driver.state.position
    return finish(
        "decided",
        logged=True,
        index_fields={"decided": 1, "orders": len(order_ids)},
        bar=result.get("bar"),
        signal=result.get("signal"),
        intents=intents,
        adopted=result.get("adopted"),
        position=None if position is None else position.as_dict(),
        order_ids=order_ids,
    )


def _default_clock(settings) -> Callable[[], Dict[str, Any]]:
    """The exchange clock, built here so the tick does not resolve credentials itself."""
    def call() -> Dict[str, Any]:
        return AlpacaExecutor(settings).clock()

    return call


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
            sleep(wait)
        else:
            # Behind the boundary: do not catch up, just move on to the next one. Replaying
            # the bar we are late for would place an order at a price that no longer exists.
            logger.warning("Woke %.0fs late for %s — skipping it, not replaying it", -wait, boundary)

    return records
