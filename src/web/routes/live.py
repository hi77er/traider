"""What the loop is doing, what the account holds, and which orders are working.

Three read-only endpoints for the header pill, the Live panel and the trading log page:

``GET /api/v1/loop``       -> the loop's own state: holder, next wake, last tick, last refusal
``GET /api/v1/positions``  -> what the account holds (both environments)
``GET /api/v1/orders``     -> open orders, the resting exit legs, and recent fills

Nothing here writes, and nothing here can place, amend or cancel an order. The trading
endpoints are separate (``routes/trading.py``) precisely so that "the page that shows you the
bot" cannot be the page that changes it — and so a read route can never need the trading lock.

Every route degrades instead of failing. An unconfigured account, a broker that is down or a
log that has never been written all produce a 200 with an explanation, because the dashboard
that reports the problem must be the one that keeps working: a 500 here would blank the panel
exactly when it has something to say.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from src.config.effective import get_effective_settings_dep
from src.execution import positions
from src.execution.config import execution_status
from src.web.auth import require_auth
from src.web.services import loop_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["live"],
    dependencies=[Depends(require_auth)],
)

#: The fields of Alpaca's order payload this UI shows, in the order it shows them. A
#: projection rather than the whole payload on purpose: the panel wants a stable shape, and
#: the broker's raw object carries account-level detail that has no business being proxied
#: into a browser.
_ORDER_FIELDS = (
    "id", "client_order_id", "symbol", "side", "type", "order_type", "order_class", "status",
    "qty", "filled_qty", "filled_avg_price", "limit_price", "stop_price",
    "submitted_at", "filled_at", "canceled_at", "expires_at",
)


def _order_view(order: Dict[str, Any]) -> Dict[str, Any]:
    """One order, projected. ``legs`` is one level deep, which is all a bracket has."""
    if not isinstance(order, dict):
        return {}
    view = {key: order.get(key) for key in _ORDER_FIELDS if key in order}
    legs = order.get("legs")
    if isinstance(legs, list):
        view["legs"] = [_order_view(leg) for leg in legs if isinstance(leg, dict)]
    return view


def _order_views(orders: Any) -> List[Dict[str, Any]]:
    return [_order_view(order) for order in (orders or []) if isinstance(order, dict)]


@router.get("/loop")
def get_loop(settings=Depends(get_effective_settings_dep)) -> dict:
    """The loop's state: is anything running it, and what did it last do."""
    return loop_service.status(settings)


@router.get("/positions")
def get_positions(settings=Depends(get_effective_settings_dep)) -> dict:
    """What the accounts hold — the same cached read the trading panel gates on.

    Both environments, never just the one in play: paper and live are different accounts, and
    a position in the one this screen is not pointed at is exactly the thing an operator
    needs to be told about.
    """
    accounts = positions.snapshots(settings)
    status_info = execution_status(settings)
    return {
        "ok": True,
        "env": status_info.get("env"),
        "instrument": settings.instrument,
        "positions": [account.as_dict() for account in accounts],
        "open_count": positions.total(accounts),
        "unknown_count": len(positions.unknown(accounts)),
        "describe": positions.describe(accounts),
    }


@router.get("/orders")
def get_orders(
    limit: int = Query(20, ge=1, le=200, description="how many finished orders to return"),
    settings=Depends(get_effective_settings_dep),
) -> dict:
    """Working orders, the exit legs that protect a position, and recent fills.

    ``resting`` is separated from ``open`` because the two answer different questions: an
    entry that has not filled yet is an open order, and a bracket parent is an open order,
    while only a stop or a limit leg protects anything. "Is this position protected?" is the
    question that matters, and it is not answered by "are there orders".
    """
    status_info = execution_status(settings)
    env = status_info.get("env")
    payload: Dict[str, Any] = {
        "ok": True, "env": env, "instrument": settings.instrument,
        "open": [], "resting": [], "closed": [], "protection": None,
    }
    if not status_info.get("ok"):
        # No credentials, or credentials for the wrong account type. Reported rather than
        # raised: this is the ordinary state of a fresh install.
        payload.update(ok=False, message=status_info.get("message"))
        # Still worth answering: "is what I am holding protected?" is a question about the
        # LOCAL record and needs no broker at all, so an unreachable broker must not blank
        # the one line that says the stop is missing.
        payload["protection"] = loop_service.protection(settings, env=env, legs=[])
        return payload

    try:
        executor = positions.executor_for(settings, env)
        payload["open"] = _order_views(executor.open_orders(settings.instrument))
        payload["resting"] = _order_views(executor.resting_exits(settings.instrument))
        payload["closed"] = _order_views(executor.closed_orders(settings.instrument, limit=limit))
    except Exception as exc:  # noqa: BLE001 - a broker outage must render, not 500
        logger.exception("Could not read orders for %s", settings.instrument)
        payload.update(ok=False, message=f"the broker could not be read ({exc})")

    # The verdict the panel leads with: funded by the resting legs already fetched above, so
    # one broker read answers both "what is working" and "is anything protecting it".
    payload["protection"] = loop_service.protection(settings, env=env, legs=payload["resting"])
    return payload


@router.get("/trades")
def get_trades(
    limit: Optional[int] = Query(None, ge=1, le=500, description="how many closed trades to return"),
    settings=Depends(get_effective_settings_dep),
) -> dict:
    """The round trips the loop has CLOSED, newest first.

    Read from the live store rather than from the broker: the broker knows what filled, and
    only the loop knows which bar, which signal and which stop produced each entry and exit.
    A deleted local log therefore degrades to an empty list rather than an error — which is
    the case the log page has to survive.
    """
    from src.execution import store

    name = loop_service.status(settings)["strategy"]
    rows = store.read_trades(settings, name, limit=limit)
    return {"ok": True, "strategy": name, "count": len(rows), "trades": list(reversed(rows))}
