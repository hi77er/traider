"""FastAPI application for the TRAIDER Web Portal — the dashboard process.

    .venv/bin/python -m src.web.app     # this: the dashboard
    .venv/bin/python -m src.main        # the other one: the trading loop

**This process serves HTTP and never trades.** Order placement belongs to the loop
(``src/main.py``), which runs separately and reads the same files: the trading
switch, the settings, and everything under ``data/``. See README, "Two processes".

That is why nothing here starts the loop and no ``--reload`` flag appears below:
this process is restarted constantly during development, and a restart here must
never be able to restart trading. ``tests/test_architecture.py`` keeps it that way.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.web.routes import account as account_routes
from src.web.routes import automation as automation_routes
from src.web.routes import chart as chart_routes
from src.web.routes import backtest as backtest_routes
from src.web.routes import dataset as dataset_routes
from src.web.routes import delta as delta_routes
from src.web.routes import execution as execution_routes
from src.web.routes import live as live_routes
from src.web.routes import log as log_routes
from src.web.routes import market as market_routes
from src.web.routes import pages as page_routes
from src.web.routes import report as report_routes
from src.web.routes import rules as rules_routes
from src.web.routes import signal as signal_routes
from src.web.routes import trading as trading_routes

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="TRAIDER Web Portal", version="0.1.0")


@app.middleware("http")
async def no_store_dashboard(request, call_next):
    """Never cache the dashboard's documents or their assets (dev).

    Two failures came from this, and both looked like bugs in the code:

    * ``app.js``/``style.css`` answered ``304 Not Modified`` after a restart (the mtime
      was unchanged), so the browser kept running the previous build;
    * the HTML carried an ``ETag`` but no ``Cache-Control``, so a browser was free to
      reuse the document heuristically while re-fetching the no-store assets beside it
      — a page whose MARKUP was older than its SCRIPT. That is how a button that had
      been deleted stayed on screen and did nothing when clicked: the server no longer
      served it, and the script that used to handle it was gone.

    ``no-store`` on both keeps them in step: what the page shows is what the server
    has. The cost is a local file read, which is the right trade for a dashboard whose
    only user is the person editing it.
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if request.url.path.startswith("/static/") or content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
app.include_router(page_routes.router)
app.include_router(log_routes.router)
app.include_router(dataset_routes.router)
app.include_router(delta_routes.router)
app.include_router(market_routes.router)
app.include_router(chart_routes.router)
app.include_router(account_routes.router)
app.include_router(execution_routes.router)
app.include_router(live_routes.router)
app.include_router(trading_routes.router)
app.include_router(rules_routes.router)
app.include_router(automation_routes.router)
app.include_router(signal_routes.router)
app.include_router(backtest_routes.router)
app.include_router(report_routes.router)


if __name__ == "__main__":
    import uvicorn

    from src.process_info import DASHBOARD, announce

    # Name the process in the log, so "which one am I looking at?" is one line away.
    announce(DASHBOARD)
    # Pass the app OBJECT, not "src.web.app:app": under `python -m src.web.app` this
    # module *is* ``__main__``, so the import string would import it a second time and
    # build a second app. Passing the object serves the one already loaded, and rules
    # out ``--reload`` at the same time (reload needs an import string).
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
