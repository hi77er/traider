"""The trading log page — what the bot did, and what the account looks like now.

``GET /log``  -> the page itself

The data comes from the ``/api/v1`` reads that already exist (``loop``, ``positions``,
``orders``, ``trades``, ``log``) rather than from a page-specific endpoint, because every one
of them is useful on its own and the dashboard's Trading panel reads several already. This
module exists only to serve the HTML.

**Why it is its own page rather than another dashboard panel.** The log is a scrollback:
ticks, orders and closed trades for a day, which is a table you page through rather than a
number you glance at. The report page set the same precedent for the same reason, and the
dashboard's right-hand column is for things you watch, not things you read.

**Account state first, local context second** (see ``templates/log.html``). The broker is the
truth about what is held; the loop's rows are the explanation of it. The page is built to
survive the local half being deleted — a log someone removed has to render as an empty day,
not as an error, or the page fails exactly when it is most wanted.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse


router = APIRouter()

_LOG_PAGE = Path(__file__).resolve().parents[1] / "templates" / "log.html"


@router.get("/log", include_in_schema=False)
def log_page() -> FileResponse:
    """Serve the trading log page."""
    return FileResponse(_LOG_PAGE)
